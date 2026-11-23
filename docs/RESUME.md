# Resume draft — Voxen

Do not finalize numbers until `benchmarks/results.md` contains real benchmark results.

## Draft bullets

- Built Voxen, a fully local Windows dictation tool: global hotkey recording, offline transcription with faster-whisper (CTranslate2), and clipboard paste into any focused application. WER [X] on a 10-sentence benchmark; median turnaround [X] s.
- Engineered a multi-threaded audio pipeline (PortAudio capture -> Whisper transcription -> Win32 clipboard injection) with bounded queues, retry/backoff, and a clipboard guard that avoids destroying non-text content. Median turnaround [X] s.

[WER [X]] and [median turnaround [X]] are placeholders. Fill them only after `benchmarks/results.md` has real values.

## Do not claim

- "90% accurate"
- "real-time"
- "production-ready"
- "CI passing"
- "tested on multiple machines"
- any accuracy, speed, or latency figure without a corresponding benchmark result in `benchmarks/results.md`

## Note

The benchmark scaffold (`benchmarks/`, `wer.py`, `results.md`) is in place but contains only `TBD` values. Run the benchmark on real audio before using any accuracy/speed claim on a resume or in the README.
