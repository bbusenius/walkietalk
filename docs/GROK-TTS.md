# Grok speech generation (phase 6)

Grok is now a voice option alongside local Piper. The implementation follows the
working Textinator pattern: send answer text to `POST https://api.x.ai/v1/tts`
with a subscription bearer token. Textinator owns its own OAuth login;
Walkietalk reuses its existing official `grok login` session instead. It does
not read Textinator's credentials or implement a new login flow.

Choose `tts.backend: grok` for the saved SuperGrok login, or explicitly choose
`grok_api` for `XAI_API_KEY` billing. There is no fallback between them or to Piper.
Grok Build still handles agent work; Grok Voice Transcribe still handles STT.
Neither CLI agent output nor STT is used as speech generation.

## Configuration

These fields are required in every config, including when Piper is selected.
Brad's local config has been extended without changing his selected voice,
radio, gain, settle delay, agent, wake, or STT settings:

```yaml
tts:
  backend: "grok"          # piper, grok, or grok_api
  piper_executable: "piper"
  piper_model: "~/.cache/walkietalk/piper/en_US-amy-medium.onnx"
  timeout_seconds: 30
  grok_voice: "eve"
  grok_language: "en"
  grok_speed: 1.0
  grok_api_key_env: "XAI_API_KEY"
  normalize: "off"
```

- `grok_voice`: built-in voice ID, such as `eve` or `ara`, or an available custom
  voice ID. Unknown/inaccessible IDs fail locally after the server rejects them;
  the bridge does not silently substitute another voice.
- `grok_language`: a supported language code such as `en`, or `auto`.
- `grok_speed`: `0.7` through `1.5`. This changes speaking speed, independently
  of `audio.gain`. The finished audio must still fit the radio duration limit.
- `normalize`: `off` keeps Grok's returned level; `peak` scales each synthesized
  reply to fill the WAV before `audio.gain`. With `peak`, start `audio.gain` at
  `1.0`. This does not change `play` of an existing file.
- `timeout_seconds`: whole synthesis deadline, including authentication refresh,
  connection, response download, and validation. A supervised worker also bounds
  blocked DNS/network shutdown. It has no radio or playback responsibilities.
- `grok_api_key_env`: environment variable **name**, used only by `grok_api`.
  Missing keys fail locally. API mode never reads the subscription token store.

For subscription mode, sign in as the normal user running Walkietalk:

```sh
grok login
```

Walkietalk reads `~/.grok/auth.json` (or `$GROK_HOME/auth.json`). It refreshes an
expired CLI grant and retries a rejected login once on HTTP 401. HTTP 403 is an
access error, not permission to use API credits. STT reloads the shared login
before each use so it picks up tokens refreshed by TTS or the CLI.

The API alternative is **separately billed**: select `grok_api` and set
`XAI_API_KEY` in your environment. Do not paste a key into YAML. The API-key route
has automated coverage; a real API-key synthesis check has not been performed.

See [xAI's TTS guide and voice previews](https://docs.x.ai/developers/model-capabilities/audio/text-to-speech)
for available voices, languages, and speed options. Textinator's working local
implementation was also inspected before this adapter was built.

## Complete Grok demonstration checkpoint

This supplements the [full phase 6 checklist](PHASE6-DEMO.md). Local generation
and automated failures are separate from family RF observations. Brad confirmed
Grok speech with the girls and authorized phase 6 publication.

1. In `config.local.yaml`, change **only** `tts.backend` to `grok`. Keep the new
   fields above. With the saved login already present, create a short WAV:

   ```sh
   mkdir -p recordings
   .venv/bin/walkietalk -c config.local.yaml tts-check "Hello. This is Grok speaking through Walkietalk." --output recordings/phase6-grok.wav
   ```

   Expect `Voice: grok (eve; speed=1; SuperGrok saved login)` and a short 48 kHz
   mono PCM16 `Speech WAV`, followed by `No hardware opened.` TX stays off.
   Choose a new output filename if one already exists; exports never overwrite.

2. Hear the same WAV on the Midland:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml play recordings/phase6-grok.wav --transmit
   ```

   Expect the full sentence, followed by TX off. Check volume and the first word.
   `audio.gain` and `radio.settle_seconds` apply to Grok exactly as to Piper.

3. Run the complete radio loop:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --transmit
   ```

   Ask a named question using your configured wake phrase. Expect the selected
   agent's answer in Grok's voice, then `Spoken reply finished; PTT released.`
   In conversation mode, ask an unaddressed follow-up after the answer ends.
   It must use the same agent context; the follow-up window starts after unkey.
   Leave it quiet afterward: no answer to its own voice, TX off, still listening.

4. Stop with Ctrl+C. Change `tts.backend` back to `piper`, restart the same command,
   and ask the same named question. Expect Amy again, with the same agent, STT,
   wake, and radio configuration. Restore whichever voice you prefer afterward.

5. Run controlled tests without accounts or radio access:

   ```sh
   .venv/bin/pytest tests/test_grok_tts.py tests/test_grok_tts_worker.py -q
   ```

   Expect all tests passing. These exercise missing login/key, rejected access,
   timeout, slow response, invalid/oversized audio, token rotation, credential
   isolation, and worker termination. A test explicitly sends a failed Grok
   synthesis through `talk --transmit` with hardware access forbidden and checks
   that no answer is printed. Existing phase 6 tests also cover continuous
   recovery/history and PTT release on playback failures or interruption.

6. Explain together: “The helper chooses the words. We can choose Amy or Grok
   to say those same words. The bridge still controls the radio's talk button.”

The bridge generates the complete WAV before PTT opens. It handles xAI's streamed
WAV header by deriving its length from the bounded response, then applies the
same strict PCM and duration checks as Piper. Server diagnostics and credentials
never become speech. Speech failures return continuous mode to listening with a
fresh wake required; there is no silent backend fallback.

## Verification results

The completed adapter generated a real **3.430-second, 48 kHz mono PCM16 WAV**
with Eve using Brad’s saved Grok subscription login. No API-key fallback or
hardware access occurred. All **549 tests** pass, including 45 Grok TTS and
worker tests; Ruff lint, formatting, and source/wheel build pass. Brad confirms
Grok speech on the radio with the girls. Individual follow-up and switch-back
observations were not separately itemized. The optional API-key route was tested
with fake HTTP only.
