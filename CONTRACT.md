# Integration contract v1

All application modules are newly written. Never copy/read old engine implementation
for implementation reuse. TASK.md is authoritative; user approved parallel independent
domain/UI development against explicit stub while new dialectical AI is built.

Python models: app/contracts.py. JSON enum keys serialize as their string values.
All responses are direct objects/arrays, NOT wrapped in data. Errors: 4xx {detail}.

## HTTP (all /api)

- POST /tasks {text, industry} -> Task
- GET /tasks -> Task[] (business workspace)
- GET /tasks/{id} -> Task
- POST /tasks/{id}/analyze {mode?: stub|engine} -> RunState (202)
- GET /tasks/{id}/run -> RunState
- POST /tasks/{id}/answers {answers:[{answer_id,answer}]} -> RunState (202)
- PUT /tasks/{id}/card {fields:{field:{value,confirmed}}} -> Task
- POST /tasks/{id}/publish -> Task
- GET /catalog?topic=&level= -> Task[]
- GET /teams -> Team[]; GET /leaderboard -> Team[]
- POST /tasks/{id}/proposals {team_id,idea,plan,deadline,link} -> Proposal
- GET /tasks/{id}/proposals -> Proposal[]
- POST /proposals/{id}/decision {decision:accept|reject} -> Proposal
- POST /proposals/{id}/stage {confirmed:true} -> Proposal
- GET /ai/contract -> object containing role, tool_schemas, examples, validation
- GET /health -> {status:ok, ai_mode:stub|engine, version:1}

## Python integration

Store(path: str), methods synchronous: create_task(text, industry)->Task,
get_task(id)->Task (KeyError absent), save_task(task)->None, list_tasks()->list[Task],
list_teams()->list[Team], get_team(id)->Team, save_team(team)->None,
create_proposal(task_id, ProposalCreate)->Proposal, list_proposals(task_id)->list[Proposal],
get_proposal(id)->Proposal, save_proposal(proposal)->None.
Store.seed() loads seed/data.json idempotently (lead supplies data); store validates.
Store.confirm_stage(proposal_id)->Proposal atomically awards 10 once to accepted team.

score(card: Card)->Score in app/scoring.py. Catalog function may be internal.

AI integration app/ai/service.py: TaskRunService(store), start(task_id,mode=None)->RunState
(called inside active asyncio loop, nonblocking), get(task_id)->RunState,
async submit_answers(task_id, answers:list[Answer])->RunState, async close()->None,
contract()->dict. ValueError = invalid transition (409); KeyError = 404.
Service saves verified card_draft and sources to store; never confirms or publishes.
One active run/task; two question rounds maximum; all card changes invalidate stale runs:
API rejects card edits while run active (409); a new run starts from latest card/sources.
Contact field is not sent to models; manual input remains available.
Stub mode explicitly labelled, shares source validation. No automatic fake fallback.

app/api/__init__.py exports create_app(db_path=None, ai_mode=None)->FastAPI.
Lead app/main.py imports create_app; launcher serves built web/dist as SPA.

## Ownership

A: app/ai/**, tests/test_ai*.py, tests/live/**.
B: app/scoring.py, app/store.py, app/catalog.py, app/api/**, tests/test_domain*.py.
C: web/**.
Lead: contracts, scaffold, seed, docs, scripts, e2e, integration fixes.
Agents use separate worktrees and branches, commit their own files for lead cherry-pick.
No push. Do not modify contracts without lead coordination.

## Fact integrity

AI values are extractive: exact normalized quotation(s), not unsupported paraphrases.
Every quote must occur in referenced user text. No new numbers/names or invented claims.
Human edits become user sources. A source supports provenance, not objective truth.
Unknown, ambiguous and conflicting assertions remain distinguished. No forced contradictions.
Confirmed stage is idempotent; low score never blocks publication or proposal.
