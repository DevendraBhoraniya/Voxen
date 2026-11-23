# Voxen benchmarks

This directory holds the accuracy/speed benchmark for Voxen. It is a scaffold:
no real numbers are present yet. Fill `benchmarks/results.md` only after
running the benchmark on real audio.

## How to run

1. Record a spoken sentence and save it as audio (e.g. `audio/s01.wav`).
2. Write the exact reference sentence into `reference/s01.txt`.
3. Transcribe the audio with Voxen and save the hypothesis to
   `hypothesis/s01.txt`.
4. Compute WER:

   ```powershell
   uv run python benchmarks/wer.py reference/s01.txt hypothesis/s01.txt
   ```
5. Record Duration (s), Reference chars, WER, and Turnaround (s) in
   `benchmarks/results.md`.

## WER

Word Error Rate (WER) measures how many words were substituted, deleted, or
inserted relative to the reference, divided by the number of reference words.
Lower is better. `0.0000` means a perfect match; `1.0000` means every word
was wrong.

## Audio / reference pairing

Each sentence is a pair: `reference/sNN.txt` holds the ground-truth text the
speaker said, and `audio/sNN.wav` (not committed) holds the corresponding
recording. The hypothesis is Voxen's transcript of that audio. Keep the
numbering aligned (s01 .. s10).
