"""Compute Word Error Rate (WER) between a reference and hypothesis transcript.

Usage:
    python benchmarks/wer.py <reference.txt> <hypothesis.txt>

Prints the WER after normalizing case and punctuation. Exits nonzero if
either file still contains the TODO placeholder (i.e. the benchmark is not
ready to run).
"""

import sys

import jiwer

TODO_PLACEHOLDER = "TODO: write reference sentence"


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "Usage: python wer.py <reference.txt> <hypothesis.txt>",
            file=sys.stderr,
        )
        return 2

    ref_path, hyp_path = sys.argv[1], sys.argv[2]

    with open(ref_path, "r", encoding="utf-8") as f:
        reference = f.read()
    with open(hyp_path, "r", encoding="utf-8") as f:
        hypothesis = f.read()

    if TODO_PLACEHOLDER in reference or TODO_PLACEHOLDER in hypothesis:
        print(
            "Refusing to compute WER: a file still contains the TODO "
            "placeholder. Replace the placeholder text with a real "
            "reference/hypothesis first.",
            file=sys.stderr,
        )
        return 1

    transformation = jiwer.Compose(
        [
            jiwer.ToLowerCase(),
            jiwer.RemovePunctuation(),
            jiwer.RemoveMultipleSpaces(),
            jiwer.Strip(),
        ]
    )
    score = jiwer.wer(
        reference,
        hypothesis,
        truth_transform=transformation,
        hypothesis_transform=transformation,
    )
    print(f"WER: {score:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
