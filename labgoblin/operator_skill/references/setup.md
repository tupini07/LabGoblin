# Explicit local setup

The video tools are optional and separate from the harness/scientific environments.
Nothing is installed by `init`, `operator-init`, a watcher or a review job.
After the human authorizes setup, use Python 3.11-3.13 to create an operator-local
environment, install this skill's pinned `requirements.txt`, and run
`scripts/prepare_models.py OPERATOR/models/kokoro`.
This explicitly downloads public Kokoro ONNX weights/voices from the upstream
project; synthesis then runs locally without Azure or another speech API.
Keep `models.json` with the download URLs, sizes and hashes.

For example, from the operator folder in PowerShell, using the selected Python:

```powershell
& $python -m venv .media
& .\.media\Scripts\python.exe -m pip install -r .\.github\skills\labgoblin-review\requirements.txt
& .\.media\Scripts\python.exe .\.github\skills\labgoblin-review\scripts\prepare_models.py .\models\kokoro
```

Then use the bound LabGoblin CLI (which must contain the review feature):

```powershell
& $lg review $project --operator-dir $operator --run EXACT_RUN_ID --credits APPROVED_REVIEW_CAP --media-python "$operator\.media\Scripts\python.exe" --models "$operator\models\kokoro"
```

Add `--watch` when arming this for an executing campaign. Waiting is outside the
production deadline; the one 90-minute clock starts on settlement. A watcher
must remain running and does not survive OS restart. It never follows another
execution or automatically retries. `--previous-review` imports an existing
written review as the initial comparison; subsequent ready journal entries are
selected automatically. `--revision 2` explicitly requests another separately
funded attempt instead of overwriting revision 1. When importing an older review,
use `--since-run` to identify its exact terminal execution; otherwise the first
job includes the campaign's entire available execution history.

The worker is a trusted shell-capable Copilot operator, not an adversarial-code
sandbox. Path tool rules do not constrain shell writes; the skill prohibits
research mutation and the host checks captured-source integrity. No hidden
claim of filesystem isolation is made. Local extensions must preserve these
boundaries and the deadline.
