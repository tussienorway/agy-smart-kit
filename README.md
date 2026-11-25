# agy-smart-kit v2.1

Gjør **Antigravity CLI (`agy`)** smart ut av boksen: automatisk **capability-discovery** (skills, plugins, MCP-servere) + **to-stegs prompt-raffinering** (**Gemini → Claude**, med **Gemini Neon critic** som fallback ved 429/kvote, og **lokal nøkkelord-ranker** hvis helt offline) før hver ikke-trivielle melding.

- **Prosjektside & direkte nedlasting:** https://tussienorway.github.io/agy-smart-kit/
- **GitHub-repo:** https://github.com/tussienorway/agy-smart-kit

---

## Krav
- **Antigravity CLI (`agy`)** med `PreInvocation`-hooks (sjekk med `/hooks` inne i `agy`)
- **Python 3.9+** og **Bash** (kun Python standardbibliotek — ingen eksterne pip-pakker)
- Anbefalt: `agy plugin install https://github.com/obra/superpowers`

---

## Rask installasjon

### Alternativ A: Klon fra GitHub (anbefalt)
```bash
git clone https://github.com/tussienorway/agy-smart-kit.git ~/agy-smart-kit
cd ~/agy-smart-kit
bash install.sh
```

### Alternativ B: Last ned ZIP fra tussienorway.github.io
```bash
curl -fsSL https://tussienorway.github.io/agy-smart-kit/downloads/agy-smart-kit.zip -o agy-smart-kit.zip
unzip agy-smart-kit.zip -d ~/agy-smart-kit
cd ~/agy-smart-kit
bash install.sh
```

### Hva `install.sh` gjør
- Kopierer skill + skript til `~/.gemini/config/skills/auto-capability-and-prompt-pipeline/`
- Fletter `PreInvocation`-hooken inn i `~/.gemini/config/hooks.json` i Antigravity CLI sitt native hook-gruppeformat (bevarer alltid andre hooks og tar backup først)
- Legger til eller oppdaterer `smart-pipeline:v2`-blokken i `~/.gemini/config/GEMINI.md` (med backup først)
- Oppretter `~/.gemini/config/pipeline.json` hvis den ikke finnes (beholder dine innstillinger ved re-installasjon)
- Kjører syntaks-, JSON-, hook- og enhetstester (`verify.sh`)

Start `agy` på nytt etter at du ser `INSTALL_OK`.

---

## Steg for steg (kan limes rett inn i en agent)
1. Klon repoet (`git clone https://github.com/tussienorway/agy-smart-kit.git ~/agy-smart-kit`) eller pakk ut `agy-smart-kit.zip`, og gå inn i mappen.
2. Kjør `bash install.sh` og bekreft at den avslutter med `INSTALL_OK`.
3. Kjør `cat ~/.gemini/config/hooks.json` og bekreft at `auto-capability-and-prompt-pipeline.PreInvocation` peker til `pre_invocation_hook.py`.
4. Test triviell utfiltrering lokalt:
   ```bash
   echo '{"lastUserInput":"hei","conversationId":"test"}' | python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pre_invocation_hook.py
   ```
   Svaret skal være `{}`.
5. Kjør `bash verify.sh --live` for en ekte ende-til-ende-test mot `agy` (bruker litt modellkvote).
6. Start `agy` på nytt. Send først `hei` (skal svare uten forsinkelse), deretter en ekte kodeoppgave — hooken fyrer og injiserer en `[SMART-PIPELINE v2]`-blokk via `injectSteps`.
7. Sjekk telemetri og status:
   ```bash
   python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --status
   ```
8. Finjuster `~/.gemini/config/pipeline.json` (modeller, timeouts, budsjett, modus) ved behov.

---

## Konfigurasjon (`~/.gemini/config/pipeline.json`)

| Nøkkel | Standard | Betydning |
|---|---|---|
| `mode` | `auto` | `auto` = hopp over små/trivielle meldinger lokalt, `always` = kjør på alle reelle meldinger, `off` = helt av |
| `hook_group` | `auto-capability-and-prompt-pipeline` | Toppnivå-nøkkel i Antigravity `hooks.json` |
| `hook_event` | `PreInvocation` | Navnet på hook-eventet i din `agy`-versjon |
| `hook_output_format` | `agy_inject_steps` | `agy_inject_steps` for Antigravity CLI (`injectSteps`), eller `additional_context` for flate JSON-nøkler |
| `hook_context_key` | `additionalContext` | JSON-nøkkel når `hook_output_format` er `additional_context` |
| `gemini_models` | `gemini-4-neon-high` → … | Fallback-kjede for Stage 1 |
| `claude_models` | `claude-sonnet-5-5-high` → … | Stage 2; avbrytes umiddelbart ved første 429/kvote |
| `critic_model` | `gemini-4-neon-high` | Stage 2 critic-fallback når Claude-kvoten er tom |
| `stage_timeout_sec` / `total_budget_sec` | `60` / `150` | Tidsgrense per steg og totalt tidsbudsjett |
| `max_inject_bytes` | `6144` | Maks størrelse (byte) på injisert blokk |
| `triage_word_limit` | `8` | Ordgrense for lokal utvelgelse i `auto`-modus |
| `catalog_cache_sec` | `900` | Hvor lenge workspace-spesifikk skill-katalog caches (15 min) |
| `refined_cache_sec` | `1800` | Hvor lenge vellykkede raffinerte prompter caches (30 min) |
| `circuit_breaker_fails` | `3` | Åpner kretsbryteren (hopper over resten av sesjonen) etter N feil på rad |

**Miljøvariabler som overstyrer config:**
- `AGY_PIPELINE_MODE` (`auto` / `always` / `off`)
- `AGY_GEMINI_MODEL` (prioritert Stage 1-modell)
- `AGY_CLAUDE_MODEL` (prioritert Stage 2-modell)
- `AGY_MAX_BYTES` (maks byte i injeksjon)
- `AGY_PIPELINE_ACTIVE=1` (hard rekursjonsvakt)

---

## Nyttige kommandoer
```bash
# Se status, ventetid, treffrate og kretsbryter
python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --status

# List alle oppdagede skills, plugins og MCP-servere for gjeldende mappe
python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --discover

# Tøm cache (nyttig etter å ha lagt til nye skills eller plugins)
python3 ~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/pipeline.py --clear-cache

# Kjør manuelt på en konkret oppgave
~/.gemini/config/skills/auto-capability-and-prompt-pipeline/scripts/prepare_task.sh "Lag en REST-klient med retry og tester"

# Kjør full testpakke lokalt
python3 -m unittest discover -s tests -v
```

---

## Feilsøking
- **Hooken fyrer ikke:** Sjekk `/hooks` inne i `agy`, og verifiser at `~/.gemini/config/hooks.json` har `"auto-capability-and-prompt-pipeline": { "PreInvocation": [...] }`.
- **Ny skill vises ikke:** Kjør `pipeline.py --clear-cache` (katalogen caches i 15 min per workspace).
- **Se hva som skjer:** Kjør `pipeline.py --status` og sjekk `~/.cache/agy-pipeline/pipeline.log`.
- **Claude 429 / kvote tom:** Helt normalt — pipen bytter automatisk til Gemini Neon critic i Stage 2.
- **Kretsbryter slått ut:** Etter 3 feil på rad i samme sesjon hopper pipen over resten av sesjonen så `agy` aldri henger. Tøm med `--clear-cache` eller start en ny sesjon.

---

## Avinstallering
```bash
bash uninstall.sh          # Beholder ~/.gemini/config/pipeline.json
bash uninstall.sh --purge  # Fjerner også pipeline.json
```
Begge tar automatisk backup (`.bak-TIMESTAMP`) av `hooks.json` og `GEMINI.md` før redigering, og rører aldri andre hooks.

---

## Hva som er fikset og ferdigstilt i v2.1.0
- **Fikset Antigravity CLI-kompatibilitet (kritiske feil fra utkastet):**
  - `--print-timeout` sender nå Go-varighet med enhet (`60s` i stedet for `60`, som tidligere feilet med exit code 2).
  - `extract_prompt()` leser nå Antigravitys faktiske felter (`lastUserInput`, `conversationId`, `cwd`, `workspacePaths`) i tillegg til generiske nøkler.
  - Hook-utdata bruker nå Antigravitys native `injectSteps` (`systemMessage`) som standard (med valgfri `additional_context`-modus).
  - `install.sh` og `config/hooks.example.json` bruker nå Antigravitys faktiske toppnivå hook-gruppeformat i `hooks.json`.
- **Fikset capability-discovery:**
  - Støtte for flerlags YAML-beskrivelser (`description: >-` / `|`) i `SKILL.md`.
  - Går hele mappestien opp til git-rot og sjekker `.agents/skills`, `.agent/skills` og `.gemini/skills`.
  - Workspace-spesifikk katalog-cache så bytte av repo ikke lekker gamle prosjektskills.
  - Korrekt parsing av `agy plugin list` (ignorerer innrykkede `Components:`/`Source:`-linjer) og `agy mcp list` (registrerer ikke `"No MCP servers configured."` som en server kalt `No`).
  - Sender eksakte filstier inn i katalogen til Stage 1 så modellene kan returnere verifiserbare stier.
- **Fikset triage, kretsbryter, cache og norsk støtte:**
  - Ordgrense på trivielle prefikser (`hi` hopper ikke lenger over `"history of git"`, `no` hopper ikke over `"node server crash"`).
  - Bare rene slash-kommandoer (`/help`, `/compact`) hoppes over — slash-kommandoer med ekte oppgave (f.eks. `/boost ...`) kjører normalt.
  - Kretsbryteren trigges nå faktisk ved degradert/feilet kjøring, og feilede fallback-svar caches ikke i 30 min.
  - Dedup-markør skrives før modellkall så verktøy-looper ikke dobbeltkjører pipen.
  - Lokal nøkkelord-ranker støtter nå `æ`, `ø`, `å` og vekter skill-navn høyere enn brødtekst.
- **Ny `uninstall.sh` + komplett enhetstestpakke (`tests/test_pipeline.py`).**
