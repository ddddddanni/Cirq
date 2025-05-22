# Copyright 2025 The Cirq Developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools for running circuits in a shuffled order with readout error benchmarking."""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import sympy

from cirq import circuits, ops, protocols, study, work
from cirq.experiments import SingleQubitReadoutCalibrationResult

if TYPE_CHECKING:
    from cirq.study import ResultDict


def _validate_input(
    input_circuits: Sequence[circuits.Circuit],
    circuit_repetitions: Union[int, list[int]],
    rng_or_seed: Union[np.random.Generator, int],
    num_random_bitstrings: int,
    readout_repetitions: int,
):
    if not input_circuits:
        raise ValueError("Input circuits must not be empty.")
    # Check input_circuits type is cirq.circuits
    if not all(isinstance(circuit, circuits.Circuit) for circuit in input_circuits):
        raise ValueError("Input circuits must be of type cirq.Circuit.")
    # Check input_circuits have measurements
    for circuit in input_circuits:
        if not any(protocols.is_measurement(circuit) for op in circuit.all_operations()):
            raise ValueError("Input circuits must have measurements.")

    # Check circuit_repetitions
    if isinstance(circuit_repetitions, int):
        if circuit_repetitions <= 0:
            raise ValueError("Must provide non-zero circuit_repetitions.")
    if isinstance(circuit_repetitions, list) and len(circuit_repetitions) != len(input_circuits):
        raise ValueError("Number of circuit_repetitions must match the number of input circuits.")

    # Check rng is a numpy random generator
    if not isinstance(rng_or_seed, np.random.Generator) and not isinstance(rng_or_seed, int):
        raise ValueError("Must provide a numpy random generator or a seed")

    # Check num_random_bitstrings is bigger than or equal to 0
    if num_random_bitstrings < 0:
        raise ValueError("Must provide zero or more num_random_bitstrings.")

    # Check readout_repetitions is bigger than 0
    if readout_repetitions <= 0:
        raise ValueError("Must provide non-zero readout_repetitions for readout calibration.")


def _validate_input_with_sweep(
    input_circuits: Sequence[circuits.Circuit],
    sweep_params: Sequence[study.Sweepable],
    circuit_repetitions: Union[int, list[int]],
    rng_or_seed: Union[np.random.Generator, int],
    num_random_bitstrings: int,
    readout_repetitions: int,
):
    """Validates the input for the run_sweep_with_readout_benchmarking function."""
    if not sweep_params:
        raise ValueError("Sweep parameters must not be empty.")
    return _validate_input(
        input_circuits, circuit_repetitions, rng_or_seed, num_random_bitstrings, readout_repetitions
    )


def _generate_readout_calibration_circuits(
    qubits: list[ops.Qid], rng: np.random.Generator, num_random_bitstrings: int
) -> tuple[list[circuits.Circuit], np.ndarray]:
    """Generates the readout calibration circuits with random bitstrings."""
    bit_to_gate = (ops.I, ops.X)

    random_bitstrings = rng.integers(0, 2, size=(num_random_bitstrings, len(qubits)))

    readout_calibration_circuits = []
    for bitstr in random_bitstrings:
        readout_calibration_circuits.append(
            circuits.Circuit(
                [bit_to_gate[bit](qubit) for bit, qubit in zip(bitstr, qubits)]
                + [ops.M(qubits, key="m")]
            )
        )
    return readout_calibration_circuits, random_bitstrings


def _generate_parameterized_readout_calibration_circuit_with_sweep(
    qubits: list[ops.Qid], rng: np.random.Generator, num_random_bitstrings: int
) -> tuple[circuits.Circuit, study.Sweepable, np.ndarray]:
    """Generates a parameterized readout calibration circuit, sweep parameters,
    and the random bitstrings."""
    random_bitstrings = rng.integers(0, 2, size=(num_random_bitstrings, len(qubits)))

    exp_symbols = [sympy.Symbol(f'exp_{qubit}') for qubit in qubits]
    parameterized_readout_calibration_circuit = circuits.Circuit(
        [ops.X(qubit) ** exp for exp, qubit in zip(exp_symbols, qubits)], ops.M(*qubits, key="m")
    )
    sweep_params = []
    for bitstr in random_bitstrings:
        sweep_params.append({exp: bit for exp, bit in zip(exp_symbols, bitstr)})

    return parameterized_readout_calibration_circuit, sweep_params, random_bitstrings


def _generate_all_readout_calibration_circuits(
    rng: np.random.Generator,
    num_random_bitstrings: int,
    qubits_to_measure: List[List[ops.Qid]],
    is_sweep: bool,
) -> Tuple[List[circuits.Circuit], List[np.ndarray], List[study.Sweepable]]:
    """Generates all readout calibration circuits and random bitstrings."""
    all_readout_calibration_circuits: list[circuits.Circuit] = []
    all_random_bitstrings: list[np.ndarray] = []
    all_readout_sweep_params: list[study.Sweepable] = []

    if num_random_bitstrings <= 0:
        return all_readout_calibration_circuits, all_random_bitstrings, all_readout_sweep_params

    if not is_sweep:
        for qubit_group in qubits_to_measure:
            readout_calibration_circuits, random_bitstrings = (
                _generate_readout_calibration_circuits(qubit_group, rng, num_random_bitstrings)
            )
            all_readout_calibration_circuits.extend(readout_calibration_circuits)
            all_random_bitstrings.append(random_bitstrings)
    else:
        for qubit_group in qubits_to_measure:
            (parameterized_readout_calibration_circuit, readout_sweep_params, random_bitstrings) = (
                _generate_parameterized_readout_calibration_circuit_with_sweep(
                    qubit_group, rng, num_random_bitstrings
                )
            )
            all_readout_calibration_circuits.append(parameterized_readout_calibration_circuit)
            all_readout_sweep_params.append([readout_sweep_params])
            all_random_bitstrings.append(random_bitstrings)

    return all_readout_calibration_circuits, all_random_bitstrings, all_readout_sweep_params


def _determine_qubits_to_measure(
    input_circuits: Sequence[circuits.Circuit],
    qubits: Optional[Union[Sequence[ops.Qid], Sequence[Sequence[ops.Qid]]]],
) -> List[List[ops.Qid]]:
    """Determine the qubits to measure based on the input circuits and provided qubits."""
    # If input qubits is None, extract qubits from input circuits
    qubits_to_measure: List[List[ops.Qid]] = []
    if qubits is None:
        qubits_set: set[ops.Qid] = set()
        for circuit in input_circuits:
            qubits_set.update(circuit.all_qubits())
        qubits_to_measure = [sorted(qubits_set)]

    elif isinstance(qubits[0], ops.Qid):
        qubits_to_measure = [qubits]  # type: ignore
    else:
        qubits_to_measure = qubits  # type: ignore
    return qubits_to_measure


def _shuffle_circuits(
    all_circuits: list[circuits.Circuit], all_repetitions: list[int], rng: np.random.Generator
) -> tuple[list[circuits.Circuit], list[int], np.ndarray]:
    """Shuffles the input circuits and readout calibration circuits."""
    shuf_order = rng.permutation(len(all_circuits))
    unshuf_order = np.zeros_like(shuf_order)  # Inverse permutation
    unshuf_order[shuf_order] = np.arange(len(all_circuits))
    shuffled_circuits = [all_circuits[i] for i in shuf_order]
    all_repetitions = [all_repetitions[i] for i in shuf_order]
    return shuffled_circuits, all_repetitions, unshuf_order


def _analyze_readout_results(
    unshuffled_readout_measurements: Union[Sequence[ResultDict], Sequence[study.Result]],
    random_bitstrings: np.ndarray,
    readout_repetitions: int,
    qubits: list[ops.Qid],
    timestamp: float,
) -> SingleQubitReadoutCalibrationResult:
    """Analyzes the readout error rates from the unshuffled measurements.

    Args:
        readout_measurements: A list of dictionaries containing the measurement results
                              for each readout calibration circuit.
        random_bitstrings: A numpy array of random bitstrings used for measuring readout.
        readout_repetitions: The number of repetitions for each readout bitstring.
        qubits: The list of qubits for which the readout error rates are to be calculated.

    Returns:
        A dictionary mapping each qubit to a tuple of readout error rates(e0 and e1),
        where e0 is the 0->1 readout error rate and e1 is the 1->0 readout error rate.
    """

    zero_state_trials = np.zeros((1, len(qubits)), dtype=np.int64)
    one_state_trials = np.zeros((1, len(qubits)), dtype=np.int64)
    zero_state_totals = np.zeros((1, len(qubits)), dtype=np.int64)
    one_state_totals = np.zeros((1, len(qubits)), dtype=np.int64)
    for measurement_result, bitstr in zip(unshuffled_readout_measurements, random_bitstrings):
        for _, trial_result in measurement_result.measurements.items():
            trial_result = trial_result.astype(np.int64)  # Cast to int64
            sample_counts = np.sum(trial_result, axis=0)

            zero_state_trials += sample_counts * (1 - bitstr)
            zero_state_totals += readout_repetitions * (1 - bitstr)
            one_state_trials += (readout_repetitions - sample_counts) * bitstr
            one_state_totals += readout_repetitions * bitstr

    zero_state_errors = {
        q: (
            zero_state_trials[0][qubit_idx] / zero_state_totals[0][qubit_idx]
            if zero_state_totals[0][qubit_idx] > 0
            else np.nan
        )
        for qubit_idx, q in enumerate(qubits)
    }

    one_state_errors = {
        q: (
            one_state_trials[0][qubit_idx] / one_state_totals[0][qubit_idx]
            if one_state_totals[0][qubit_idx] > 0
            else np.nan
        )
        for qubit_idx, q in enumerate(qubits)
    }
    return SingleQubitReadoutCalibrationResult(
        zero_state_errors=zero_state_errors,
        one_state_errors=one_state_errors,
        repetitions=readout_repetitions,
        timestamp=timestamp,
    )


def run_shuffled_with_readout_benchmarking(
    input_circuits: list[circuits.Circuit],
    sampler: work.Sampler,
    circuit_repetitions: int | list[int],
    rng_or_seed: np.random.Generator | int,
    num_random_bitstrings: int = 100,
    readout_repetitions: int = 1000,
    qubits: Optional[Union[Sequence[ops.Qid], Sequence[Sequence[ops.Qid]]]] = None,
) -> tuple[list[ResultDict], Dict[Tuple[ops.Qid, ...], SingleQubitReadoutCalibrationResult]]:

    """Run the circuits in a shuffled order with readout error benchmarking.

    Args:
        input_circuits: The circuits to run.
        sampler: The sampler to use.
        circuit_repetitions: The repetitions for `circuits`.
        rng_or_seed: A random number generator used to generate readout circuits.
                     Or an integer seed.
        num_random_bitstrings: The number of random bitstrings for measuring readout.
            If set to 0, no readout calibration circuits are generated.
        readout_repetitions: The number of repetitions for each readout bitstring.
        qubits: The qubits to benchmark readout errors. If None, all qubits in the
                input_circuits are used. Can be a list of qubits or a list of tuples
                of qubits.

    Returns:
        A tuple containing:
        - A list of dictionaries with the unshuffled measurement results.
        - A dictionary mapping each tuple of qubits to a SingleQubitReadoutCalibrationResult.

    """

    _validate_input(
        input_circuits, circuit_repetitions, rng_or_seed, num_random_bitstrings, readout_repetitions
    )

    qubits_to_measure = _determine_qubits_to_measure(input_circuits, qubits)

    # Generate the readout calibration circuits if num_random_bitstrings>0
    # Else all_readout_calibration_circuits and all_random_bitstrings are empty
    rng = (
        rng_or_seed
        if isinstance(rng_or_seed, np.random.Generator)
        else np.random.default_rng(rng_or_seed)
    )

    all_readout_calibration_circuits, all_random_bitstrings, _ = (
        _generate_all_readout_calibration_circuits(
            rng, num_random_bitstrings, qubits_to_measure, False
        )
    )

    # Shuffle the circuits
    if isinstance(circuit_repetitions, int):
        circuit_repetitions = [circuit_repetitions] * len(input_circuits)
    all_repetitions = circuit_repetitions + [readout_repetitions] * len(
        all_readout_calibration_circuits
    )

    shuffled_circuits, all_repetitions, unshuf_order = _shuffle_circuits(
        input_circuits + all_readout_calibration_circuits, all_repetitions, rng
    )

    # Run the shuffled circuits and measure
    results = sampler.run_batch(shuffled_circuits, repetitions=all_repetitions)
    timestamp = time.time()
    shuffled_measurements = [res[0] for res in results]
    unshuffled_measurements = [shuffled_measurements[i] for i in unshuf_order]

    unshuffled_input_circuits_measiurements = unshuffled_measurements[: len(input_circuits)]
    unshuffled_readout_measurements = unshuffled_measurements[len(input_circuits) :]

    # Analyze results
    readout_calibration_results = {}
    start_idx = 0
    for qubit_group, random_bitstrings in zip(qubits_to_measure, all_random_bitstrings):
        end_idx = start_idx + len(random_bitstrings)
        group_measurements = unshuffled_readout_measurements[start_idx:end_idx]
        calibration_result = _analyze_readout_results(
            group_measurements, random_bitstrings, readout_repetitions, qubit_group, timestamp
        )
        readout_calibration_results[tuple(qubit_group)] = calibration_result
        start_idx = end_idx

    return unshuffled_input_circuits_measiurements, readout_calibration_results


def run_sweep_with_readout_benchmarking(
    input_circuits: list[circuits.Circuit],
    sweep_params: Sequence[study.Sweepable],
    sampler: work.Sampler,
    circuit_repetitions: Union[int, list[int]],
    rng_or_seed: Union[np.random.Generator, int],
    num_random_bitstrings: int = 100,
    readout_repetitions: int = 1000,
    qubits: Optional[Union[Sequence[ops.Qid], Sequence[Sequence[ops.Qid]]]] = None,
) -> tuple[
    list[list[study.Result]], Dict[Tuple[ops.Qid, ...], SingleQubitReadoutCalibrationResult]
]:
    """Run the sweep circuits with readout error benchmarking (no shuffling).
    Args:
        input_circuits: The circuits to run.
        sweep_params: The sweep parameters for the input circuits.
        sampler: The sampler to use.
        circuit_repetitions: The repetitions for `circuits`.
        rng_or_seed: A random number generator used to generate readout circuits.
                     Or an integer seed.
        num_random_bitstrings: The number of random bitstrings for measuring readout.
            If set to 0, no readout calibration circuits are generated.
        readout_repetitions: The number of repetitions for each readout bitstring.
        qubits: The qubits to benchmark readout errors. If None, all qubits in the
                input_circuits are used. Can be a list of qubits or a list of tuples
                of qubits.
    Returns:
        A tuple containing:
        - A list of lists of dictionaries with the measurement results.
        - A dictionary mapping each tuple of qubits to a SingleQubitReadoutCalibrationResult.
    """

    _validate_input_with_sweep(
        input_circuits,
        sweep_params,
        circuit_repetitions,
        rng_or_seed,
        num_random_bitstrings,
        readout_repetitions,
    )

    qubits_to_measure = _determine_qubits_to_measure(input_circuits, qubits)

    # Generate the readout calibration circuits (parameterized circuits) and sweep params
    # if num_random_bitstrings>0
    # Else all_readout_calibration_circuits and all_random_bitstrings are empty
    rng = (
        rng_or_seed
        if isinstance(rng_or_seed, np.random.Generator)
        else np.random.default_rng(rng_or_seed)
    )

    all_readout_calibration_circuits, all_random_bitstrings, all_readout_sweep_params = (
        _generate_all_readout_calibration_circuits(
            rng, num_random_bitstrings, qubits_to_measure, True
        )
    )

    if isinstance(circuit_repetitions, int):
        circuit_repetitions = [circuit_repetitions] * len(input_circuits)
    all_repetitions = circuit_repetitions + [readout_repetitions] * len(
        all_readout_calibration_circuits
    )

    # Run the sweep circuits and measure
    results = sampler.run_batch(
        input_circuits + all_readout_calibration_circuits,
        list(sweep_params) + all_readout_sweep_params,
        repetitions=all_repetitions,
    )

    timestamp = time.time()

    input_circuits_measiurements = results[: len(input_circuits)]
    readout_measurements = results[len(input_circuits) :]

    # Analyze results
    readout_calibration_results = {}
    i = 0
    for qubit_group, random_bitstrings in zip(qubits_to_measure, all_random_bitstrings):
        group_measurements = readout_measurements[i]
        i += 1

        calibration_result = _analyze_readout_results(
            group_measurements, random_bitstrings, readout_repetitions, qubit_group, timestamp
        )
        readout_calibration_results[tuple(qubit_group)] = calibration_result

    return input_circuits_measiurements, readout_calibration_results
