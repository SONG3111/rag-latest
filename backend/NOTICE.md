# Third-party notices for the backend

## WhisperLiveKit

- Source: https://github.com/QuentinFuxa/WhisperLiveKit
- License: Apache License 2.0
- Used by: `app/meeting/asr.py`
- Purpose: streaming PCM input, VAD/VAC, LocalAgreement transcription, and the
  Whisper/faster-whisper backend integration.

## Meetily

- Source: https://github.com/Zackriya-Solutions/meetily
- License: MIT License
- Referenced by: `app/meeting/minutes.py`, `app/meeting/prompts.py`
- Adapted concept: chunk summaries, combine pass, final template pass, and
  action items with transcript evidence. No source code is copied verbatim.

## Anarlog (formerly Hyprnote)

- Source: https://github.com/fastrepl/anarlog
- License: MIT License
- Referenced by: `app/meeting/prompts.py`
- Adapted concept: combine transcript and user notes while requiring strictly
  grounded output and avoiding generic invented sections.

## OpenCC

- Source: https://github.com/BYVoid/OpenCC
- License: Apache License 2.0
- Used by: `app/meeting/text.py`
- Purpose: deterministic Traditional-to-Simplified Chinese conversion for
  streamed and migrated meeting transcripts.
