# samplepick: pick samples by describing them

Describe a sound in words ("a warm piano loop around 80 bpm", "dirtier drums"). An LLM judge
(TypeSafe Jev, a small decision model on OpenRouter) ranks the samples in your own library, you listen,
tick the ones you like, and the keepers are copied into a folder you can drag straight into your DAW.

- The judge reads **text only**: a sample reaches it as its file name, folder tags, pack, key and tempo
  (and, if you set up listening, a short description of how it sounds). Audio is never sent to the judge.
- Sample files are only ever **copied**, never moved or renamed. The copies stay on your machine.
- Works with names alone. Listening analysis (timbre, chords) is optional.

## Install

Python 3.10+ and nothing else (standard library only).

```
git clone <this repo> samplepick && cd samplepick
python3 -m pip install .          # optional: gives you the `samplepick` and `samplepick-server` commands
```

You can also run it straight from the folder: `python3 samplepick.py ...` and `python3 server.py`.
Tests: `python3 -m pip install pytest && python3 -m pytest -q` (the judge and chat models are faked;
no network, no cost).

Set a judge key (an OpenRouter account with a little credit):

```
export SAMPLEPICK_JUDGE_KEY=sk-or-...      # or OPENROUTER_API_KEY
```

## Point it at your library and index it

```
export SAMPLEPICK_LIBRARY="$HOME/Samples:$HOME/Downloads/Packs"    # os.pathsep-separated (";" on Windows)
python3 samplepick.py index                                       # or: index --library DIR --library DIR2
```

`index` walks the folders and writes `data/index.jsonl`: one line per audio file (wav, aif, aiff, flac,
mp3, ogg) with type (loop or oneshot), key and bpm read from file and folder names, length from the WAV
header, tags from folder names, and the pack name (the top folder, or the one below it when the top
folder only groups five or more packs). Paths are stored absolute. Re-run `index` when you add or
remove files. If a library folder is unreadable (an unplugged drive) its previous rows are kept and
skipped until it is back.

## Ask, listen, save

```
python3 samplepick.py ask "Does sample #{n} feel like Christmas?" --type loop --words bell --slot bells
python3 samplepick.py list
python3 samplepick.py reveal 3                  # show item 3 in your file manager
python3 samplepick.py save "Xmas" 1 3 7 --open  # copy items 1, 3, 7 into <root>/Xmas
```

- A question is a yes/no sentence with `#{n}` where each sample's number goes. For a choice among options:
  `--options '{"warm":"warm and soft","dark":"dark and dull"}'`.
- Narrow the scope with `--type loop|oneshot`, `--words` (words that must appear in name, tags or pack),
  `--key`, `--bpm 120-130`. One ask covers at most 3000 candidates (`MAX_ASK`); beyond that it comes back
  unasked and tells you to narrow it.
- Two passes: the judge first ranks every candidate by name and tags (batches of 300 per call, all sent at
  once); the best 30 (`SHORTLIST`) are then heard (if listening is set up) and asked again with what they
  sound like, and that second ranking is what you see. Scores are a ranking, not a threshold.
- Numbers keep counting across asks; `--new` starts a new conversation with a fresh list.
- `--like "folder/file.wav"` picks things similar to a file already in one of your save folders.

Other commands: `folders`, `look FOLDER` (files in a save folder with the index's description of each),
`rename OLD NEW`, `rmdir NAME` (moves to `<root>/.trash`, never deletes), `notes [--set TEXT]`,
`block 4 7 --why ...` / `unblock` / `blocked` (never pick these again), `stats` (how often each sample was
shown and saved). All output is JSON.

`reveal` uses Windows Explorer on WSL (`explorer.exe`), `open -R` on macOS, and `xdg-open` on the folder
on other Linux (it cannot highlight a single file).

## Web page

```
python3 server.py --port 8772        # http://127.0.0.1:8772/  (binds 127.0.0.1 only)
```

Chat with a picking partner on the left (it turns what you say into questions for the judge), a numbered
result list on the right: play, tick, show location, never-pick, save into a folder. The partner is the
`claude` or the `codex` command-line tool, used as a text-only model with **every tool switched off**; it
answers with a JSON object and this program carries out the actions. `prompt.md` is its system prompt.

- `claude`: needs Claude Code installed and logged in. Runs from an empty temp folder with hooks, slash
  commands, MCP and tools disabled. `SAMPLEPICK_CLAUDE_CONFIG_DIR` picks another login folder.
- `codex`: needs the Codex CLI and **bubblewrap** (`bwrap`; Linux/WSL: `apt install bubblewrap`). It runs
  inside a bwrap jail that shows only the system, node, and the login files of the codex home. If `bwrap`
  is missing the backend refuses to run (it never runs unjailed). `SAMPLEPICK_CODEX_HOME` picks the login
  folder (default `~/.codex`).

The chat is optional; the CLI does everything except the conversation.

## Optional: listening analysis

Without it, ranking uses names only. With it, the shortlist is described to the judge ("sounds warm, soft;
reverberant; chords: Am F").

```
export SAMPLEPICK_TIMBRE_PYTHON=/path/to/venv/bin/python   # a python that has AudioCommons `timbral_models`
export SAMPLEPICK_PHONON_URL=https://your-phonon-host/api/ # chords/tempo service (off unless set)
python3 samplepick.py analyze --type loop --words piano --limit 200   # fill data/analysis.jsonl ahead of time
```

- **Timbre** (hardness, depth, brightness, roughness, warmth, sharpness, boominess, reverb) comes from the
  AudioCommons `timbral_models` package. It needs **numpy 1.x and librosa 0.9**, so give it its own
  virtualenv and point `SAMPLEPICK_TIMBRE_PYTHON` at that venv's python. Both it and the Phonon upload need
  `ffmpeg` on your PATH (each sample is cut to its opening seconds, mono 44.1 kHz).
- **Chords** of pitched loops (and tempo when the file name has none) come from a Phonon analysis service.
  It is off unless `SAMPLEPICK_PHONON_URL` is set. A 12-second clip of each shortlisted loop is uploaded to
  that URL, so leave it unset if the audio must not leave your machine.
- Each sample is analysed once; results are cached in `data/analysis.jsonl` and used by later asks even
  if the variables are unset again.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `SAMPLEPICK_LIBRARY` | sample folders, `os.pathsep`-separated | none (`index --library` instead) |
| `SAMPLEPICK_DATA` | index, analysis, judge log, conversations, notes, stats | `./data` next to the code |
| `SAMPLEPICK_ROOT` | where save folders go | `~/Music/Sample Picks` |
| `SAMPLEPICK_JUDGE_KEY` / `OPENROUTER_API_KEY` | judge key (first one set wins) | none |
| `SAMPLEPICK_JUDGE_URL` | judge endpoint (a non-OpenRouter URL gets no Authorization header) | `https://openrouter.ai/api/alpha/decisions` |
| `SAMPLEPICK_JUDGE_MODEL` | judge model id | `typesafe/jev-1.13` |
| `SAMPLEPICK_JUDGE_TIMEOUT` | seconds per judge call | `30` |
| `SAMPLEPICK_TIMBRE_PYTHON` | python with `timbral_models` | off |
| `SAMPLEPICK_PHONON_URL` | Phonon service base URL | off |
| `SAMPLEPICK_CLAUDE_CONFIG_DIR` | Claude login folder for the chat | claude's default |
| `SAMPLEPICK_CODEX_HOME` | Codex login folder for the chat | `~/.codex` |

## Costs

Judge calls cost money on OpenRouter. From the measurements in `sample_judge.py`: a batch of 300 answers
comes back in under a second for about $0.0009, so a question costs roughly **$0.003 to $0.004 per
thousand candidates**, plus a negligible second pass over the 30-sample shortlist. An ask over the
3000-candidate cap would be about $0.01 and take around a minute and a half; a typical narrowed ask
(a few hundred candidates) is a fraction of a cent. Prices can change; the real usage and cost of every
call is in `data/judge.jsonl`. The chat partner (`claude` / `codex`) is billed or rate-limited by
whatever login that tool uses, not by this program.

## What is kept on disk (`data/`, gitignored)

`index.jsonl`, `analysis.jsonl`, `judge.jsonl` (every judge call, failed ones too: state, questions,
answers, model, usage and cost, elapsed time), `talks/` (conversations and their numbered lists),
`saved.jsonl` (what was copied where), `stats.json`, `blocked.json`, `notes.md` (+ `notes-history.jsonl`),
`calls.jsonl` (chat model calls). File names and folder names from your library appear in `judge.jsonl`
and in what is sent to the judge; the audio itself stays on your machine.

## Layout

`samplepick.py` CLI and library functions; `server.py` + `page.html` + `prompt.md` the web page and chat;
`sample_judge.py` candidates / scoring / the two-pass ask; `judge.py` the one call to the judge, logged;
`library.py` the index; `analyze.py` + `timbre_worker.py` optional listening; `config.py` paths.

## License

MIT, see [LICENSE](LICENSE). If samplepick helps your work, a mention or a link back is appreciated.
