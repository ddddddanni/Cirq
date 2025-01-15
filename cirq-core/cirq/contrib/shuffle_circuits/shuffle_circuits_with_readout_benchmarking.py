# Copyright 2019 The Cirq Developers
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

from typing import Optional

import numpy as np

from cirq import ops, circuits, work, protocols


def _validate_input(input_circuits: list[circuits.Circuit], circuit_repetitions: int | list[int],
                    rng: np.random.Generator, num_random_bitstrings: int, readout_repetitions: int):
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
    if isinstance(circuit_repetitions,list) and len(circuit_repetitions) != len(input_circuits):
        raise ValueError(
            "Number of circuit_repetitions must match the number of input circuits.")

    # Check rng is a numpy random generator
    if not isinstance(rng, np.random.Generator):
        raise ValueError("Must provide a numpy random generator")

    # Check num_random_bitstrings is bigger than 0
    if num_random_bitstrings <= 0:
        raise ValueError("Must provide non-zero num_random_bitstrings.")

    # Check readout_repetitions is bigger than 0
    if readout_repetitions <= 0:
        raise ValueError(
            "Must provide non-zero readout_repetitions for readout calibration.")


def _generate_readout_calibration_circuits(
    qubits: list["cirq.Qid"], rng: np.random.Generator, num_random_bitstrings: int
) -> tuple[list[circuits.Circuit], np.ndarray]:
    """Generates the readout calibration circuits with random bitstrings."""
    def x_or_i(bit):
        """Returns the X gate if the bit is 1, otherwise returns the I gate."""
        if bit == 1:
            return ops.X
        return ops.I

    random_bitstrings = rng.integers(0, 2, size=(
        num_random_bitstrings, len(qubits)))

    readout_calibration_circuits = []
    for bitstr in random_bitstrings:
        readout_calibration_circuits.append(
            circuits.Circuit(
                [x_or_i(bit)(qubit) for bit, qubit in zip(bitstr, qubits)]
                + [ops.M(qubits, key="m")]
            )
        )
    return readout_calibration_circuits, random_bitstrings


def _shuffle_circuits(all_circuits: list[circuits.Circuit], all_repetitions: list[int],
                      rng: np.random.Generator) -> tuple[list[circuits.Circuit],
                                                         list[int], np.ndarray]:
    """Shuffles the input circuits and readout calibration circuits."""
    shuf_order = rng.permutation(len(all_circuits))
    unshuf_order = np.zeros_like(shuf_order)
    unshuf_order[shuf_order] = np.arange(len(all_circuits))
    shuffled_circuits = [all_circuits[i] for i in shuf_order]
    all_repetitions = [all_repetitions[i] for i in shuf_order]
    return shuffled_circuits, all_repetitions, unshuf_order


def _analyze_readout_results(
    unshuffled_readout_measurements: list[np.ndarray],
    random_bitstrings: np.ndarray,
    readout_repetitions: int,
    qubits: list["cirq.Qid"],
) -> dict["cirq.Qid", tuple[float, float]]:
    """Analyzes the readout error rates from the unshuffled measurements."""
    zero_state_trials = np.zeros((1, len(qubits)), dtype=np.int64)
    one_state_trials = np.zeros((1, len(qubits)), dtype=np.int64)
    zero_state_totals = np.zeros((1, len(qubits)), dtype=np.int64)
    one_state_totals = np.zeros((1, len(qubits)), dtype=np.int64)
    trial_idx = 0
    for trial_result in unshuffled_readout_measurements:
        trial_result = trial_result.astype(np.int64)  # Cast to int64
        sample_counts = np.sum(trial_result, axis=0)

        zero_state_trials += sample_counts * (1 - random_bitstrings[trial_idx])
        zero_state_totals += readout_repetitions * \
            (1 - random_bitstrings[trial_idx])
        one_state_trials += (readout_repetitions -
                             sample_counts) * random_bitstrings[trial_idx]
        one_state_totals += readout_repetitions * random_bitstrings[trial_idx]

        trial_idx += 1

    readout_error_rates = {
        q: (
            zero_state_trials[0][qubit_idx] / zero_state_totals[0][qubit_idx]
            if zero_state_totals[0][qubit_idx] > 0
            else np.nan, one_state_trials[0][qubit_idx] / one_state_totals[0][qubit_idx]
            if one_state_totals[0][qubit_idx] > 0
            else np.nan
        )
        for qubit_idx, q in enumerate(qubits)
    }
    return readout_error_rates


def run_shuffled_with_readout_benchmarking(
    input_circuits: list[circuits.Circuit],
    sampler: work.Sampler,
    circuit_repetitions: int | list[int],
    rng: np.random.Generator,
    num_random_bitstrings: int = 100,
    readout_repetitions: int = 1000,
    qubits: Optional[list["cirq.Qid"]] = None,
) -> tuple[np.ndarray, dict["cirq.Qid": tuple[float, float]]]:
    """Run the circuits in a shuffled order with readout error benchmarking.

    Args:
        input_circuits: The circuits to run.
        sampler: The sampler to use.
        circuit_repetitions: The repetitions for `circuits`.
        rng: A random number generator used to generate readout circuits.
        num_random_bitstrings: The number of random bitstrings for measuring readout.
        readout_repetitions: The number of repetitions for each readout bitstring.
        qubits: The qubits to benchmark readout errors. If None, all qubits in the 
        input_circuits are used.

    Returns:
        The unshuffled measurements and a dictionary from qubits to the corresponding
        readout error rates (e0 and e1, where e0 is the 0->1 readout error rate and 
        e1 is the 1->0 readout error rate).

    """

    _validate_input(input_circuits, circuit_repetitions, rng,
                    num_random_bitstrings, readout_repetitions)

    # If input qubits is None, extract qubits from input circuits
    if qubits is None:
        qubits = set()
        for circuit in input_circuits:
            qubits.update(circuit.all_qubits())
        qubits = sorted(qubits)

    # Generate the readout calibration circuits
    readout_calibration_circuits, random_bitstrings = _generate_readout_calibration_circuits(
        qubits, rng, num_random_bitstrings)

    # Shuffle the circuits
    if isinstance(circuit_repetitions, int):
        circuit_repetitions = [circuit_repetitions] * len(input_circuits)
    all_repetitions = circuit_repetitions + \
        [readout_repetitions] * len(readout_calibration_circuits)

    shuffled_circuits, all_repetitions, unshuf_order = _shuffle_circuits(
        input_circuits + readout_calibration_circuits, all_repetitions, rng)

    # Run the shuffled circuits and measure
    results = sampler.run_batch(shuffled_circuits, repetitions=all_repetitions)
    shuffled_measurements = [res[0].measurements["m"] for res in results]
    unshuffled_measurements = [shuffled_measurements[i] for i in unshuf_order]

    unshuffled_readout_measurements = unshuffled_measurements[len(
        input_circuits):]

    # Analyze results
    readout_error_rates = _analyze_readout_results(
        unshuffled_readout_measurements, random_bitstrings, readout_repetitions, qubits)

    return unshuffled_measurements, readout_error_rates