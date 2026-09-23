"""State, eligibility and atomic commits. No model or provider access in this module."""
import copy
import re
from dataclasses import dataclass, field
from typing import get_args

from engines.common.contracts import FieldName, GraphEdge, GraphNode, GraphView, Question, StartRequest
from .models import Move


OPPOSITE = "Студенческая команда хочет начать работу, зная только карточку и не задавая вопросов бизнесу."


def norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"').split())


@dataclass
class State:
    req: StartRequest
    phase: str = "planning"
    counters: dict = field(default_factory=dict)
    processes: dict = field(default_factory=dict)
    designations: dict = field(default_factory=dict)
    developments: dict = field(default_factory=dict)
    contradictions: dict = field(default_factory=dict)
    resolutions: dict = field(default_factory=dict)
    roadmaps: dict = field(default_factory=dict)
    actions: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)
    assessments: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    candidate: str | None = None
    simplest: str | None = None
    opposite: str | None = None
    current_map: str | None = None
    revision_reason: str | None = None
    rounds: int = 0
    provenance_failures: int = 0
    card: object = None
    completion: dict | None = None

    def __post_init__(self):
        if not self.sources:
            self.sources = {"draft": self.req.draft}

    def ident(self, prefix):
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]}"

    def process(self, content, side=None):
        pid = self.ident("P")
        self.processes[pid] = {"id": pid, "content": content, "side": side}
        return pid

    def graph(self):
        nodes, edges = [], []
        for pid, p in self.processes.items():
            if pid == self.candidate or not p.get("side"):
                continue
            kind = "simplest" if pid == self.simplest else "opposite" if pid == self.opposite else "element"
            nodes.append(GraphNode(id=pid, kind=kind, label=p["content"], side=p["side"], status="ok"))
        for d in self.developments.values():
            edges.append(GraphEdge(source=d["source_process_id"], target=d["emergent_process_id"], kind="develops"))
        for cid, c in self.contradictions.items():
            nodes.append(GraphNode(id=cid, kind="contradiction", label=c["unity"], status=c["status"]))
            for ref in c["simplest_dev_refs"] + c["opposite_dev_refs"]:
                edges.append(GraphEdge(source=self.developments[ref]["emergent_process_id"], target=cid, kind="contradicts"))
        for qid, q in self.questions.items():
            nodes.append(GraphNode(id=qid, kind="question", label=q.text))
            edges.append(GraphEdge(source=qid, target=q.contradiction_id, kind="resolves"))
        for aid, a in self.answers.items():
            nodes.append(GraphNode(id=aid, kind="answer", label=a["text"] or "Ответ не известен"))
            edges.append(GraphEdge(source=aid, target=a["question_id"], kind="answers"))
        if self.card:
            for i, (key, value) in enumerate(self.card.fields.items(), 1):
                if value.value is None:
                    continue
                fid = f"F{i}"
                nodes.append(GraphNode(id=fid, kind="field", label=f"{key}: {value.value}", status="ok"))
                for source in value.sources:
                    origin = self.simplest if source.source_id == "draft" else source.source_id
                    edges.append(GraphEdge(source=origin, target=fid, kind="grounds"))
        return GraphView(nodes=nodes, edges=edges)


def current_observations(s):
    return [o for o in s.observations.values() if s.actions[o["action_id"]]["roadmap_id"] == s.current_map]


def unassessed(s):
    return [o for o in current_observations(s) if not any(a["observation_id"] == o["id"] for a in s.assessments.values())]


def unresolved_practice(s):
    # An assessed corrected attempt supersedes an earlier failed attempt of that tool.
    latest = {}
    for o in current_observations(s):
        latest[s.actions[o["action_id"]]["tool"]] = o
    return [o for o in latest.values() if any(a["observation_id"] == o["id"] and a["relation"] == "contradicted" for a in s.assessments.values())]


def eligible(s: State, name: str) -> bool:
    if s.phase in {"done", "failed"}:
        return False
    if name == "PROPOSE_SIMPLEST":
        return s.phase == "planning" and not s.simplest and not s.candidate
    if name == "ASSESS_SIMPLEST":
        return s.phase == "planning" and bool(s.candidate)
    if name == "DEVELOP_PROCESS":
        return s.phase == "planning" and bool(s.simplest)
    if name == "ESTABLISH_CONTRADICTION":
        return s.phase == "planning" and all(any(d["side"] == side for d in s.developments.values()) for side in ("simplest", "opposite"))
    if name == "PROPOSE_LEAP":
        return s.phase == "planning" and any(not any(r["contradiction_id"] == cid and not r.get("superseded") for r in s.resolutions.values()) for cid in s.contradictions)
    if name == "BEGIN_EXECUTION":
        pending = [r for r in s.resolutions.values() if not r.get("superseded") and not r.get("confirmed_roadmap_id")]
        return s.phase == "planning" and bool(pending) and (s.rounds > 0 or sum(len(r["questions"]) for r in pending) >= 3)
    if s.phase != "executing":
        return False
    if name == "ASSESS_PRACTICE":
        return bool(unassessed(s))
    pending_action = any(a["status"] == "pending" for a in s.actions.values())
    if pending_action or unassessed(s):
        return False
    observations = current_observations(s)
    asked = any(s.actions[o["action_id"]]["tool"] == "ask_business" and o["success"] for o in observations)
    valid_card = any(s.actions[o["action_id"]]["tool"] == "commit_card" and o["success"] for o in observations)
    if name == "ASK_BUSINESS":
        return s.rounds < 2 and not asked and bool(s.roadmaps[s.current_map]["question_ids"])
    if name == "COMMIT_CARD":
        return (asked or s.rounds >= 2) and not valid_card
    if name == "REVISE_WORLD":
        return bool(unresolved_practice(s))
    if name == "ASSESS_LEAP":
        return valid_card and not unresolved_practice(s) and any(s.resolutions[r]["confirmed_roadmap_id"] != s.current_map for r in s.roadmaps[s.current_map]["resolution_ids"])
    if name == "COMPLETE":
        return valid_card and s.rounds >= 1 and not unresolved_practice(s) and all(s.resolutions[r]["confirmed_roadmap_id"] == s.current_map for r in s.roadmaps[s.current_map]["resolution_ids"])
    return False


class Resolver:
    @staticmethod
    def allowed(s):
        from .models import PAYLOADS
        return [name for name in PAYLOADS if eligible(s, name)]


class StructuralValidator:
    @staticmethod
    def validate(move: Move, s: State):
        try:
            p = move.typed()
        except (ValueError, TypeError) as exc:
            return False, f"Payload не соответствует схеме: {exc}"
        if not eligible(s, move.move):
            return False, f"Ход {move.move} сейчас недоступен; разрешены {Resolver.allowed(s)}"
        name = move.move
        for key, value in p.model_dump().items():
            if isinstance(value, str) and key not in {"quote", "source_id"} and not value.strip():
                return False, f"Поле {key} не должно быть пустым"
        if name == "PROPOSE_SIMPLEST" and norm(p.quote) not in norm(s.req.draft):
            return False, "Цитата простейшего должна быть из черновика"
        if name == "DEVELOP_PROCESS":
            source = s.processes.get(p.source_process_id)
            if not source or source["side"] not in {"simplest", "opposite"} or p.source_process_id == s.candidate:
                return False, "Источник развития должен быть утверждённым процессом"
            if source["side"] == "simplest" and (not norm(p.quote) or p.source_id not in s.sources or norm(p.quote) not in norm(s.sources[p.source_id])):
                return False, "Развитие простейшего требует точную цитату существующего источника"
        if name == "ESTABLISH_CONTRADICTION":
            for side in ("simplest", "opposite"):
                refs = getattr(p, side + "_dev_refs")
                if any(r not in s.developments or s.developments[r]["side"] != side for r in refs):
                    return False, f"Ссылки развития должны принадлежать стороне {side}"
        if name == "PROPOSE_LEAP":
            if p.contradiction_id not in s.contradictions or any(r["contradiction_id"] == p.contradiction_id and not r.get("superseded") for r in s.resolutions.values()):
                return False, "Нужно существующее противоречие без действующего разрешения"
            if any(q.text.count("?") != 1 or not re.search("[А-Яа-яЁё]", q.text) for q in p.questions):
                return False, "Каждый вопрос должен быть на русском с одним знаком вопроса"
            if len({q.text for q in p.questions}) != len(p.questions):
                return False, "Вопросы не должны повторяться"
            if any(q.field == "contact" for q in p.questions):
                return False, "Контакт заполняется вручную, вопросы о контакте не задаются"
        if name == "BEGIN_EXECUTION":
            if len(set(p.resolution_ids)) != len(p.resolution_ids) or any(r not in s.resolutions or s.resolutions[r].get("superseded") for r in p.resolution_ids):
                return False, "Маршрут должен ссылаться на действующие разрешения без повторов"
            question_ids = [q for r in p.resolution_ids for q in s.resolutions[r]["questions"]]
            if not 3 <= len(question_ids) <= 5 and s.rounds == 0:
                return False, "Первый маршрут должен содержать 3–5 вопросов"
            if len(question_ids) > 5:
                return False, "За круг не более пяти вопросов"
            if s.current_map and p.resolution_ids == s.roadmaps[s.current_map]["resolution_ids"]:
                return False, "После пересмотра маршрут должен отличаться от прежнего"
        if name in {"ASK_BUSINESS", "COMMIT_CARD"}:
            roadmap = s.roadmaps[s.current_map]
            if p.origin_process_id not in roadmap["execution_process_ids"]:
                return False, "Источник действия должен быть процессом текущего маршрута"
            if name == "COMMIT_CARD":
                if set(p.card.fields) != set(get_args(FieldName)):
                    return False, "Карточка содержит ровно десять полей; неизвестные значения null"
                previous = [a for a in s.actions.values() if a["tool"] == "commit_card" and a["roadmap_id"] == s.current_map]
                if previous and previous[-1]["args"] == p.card.model_dump():
                    return False, "Повторная карточка должна исправлять предыдущие ошибки"
        if name == "ASSESS_PRACTICE":
            observation = next((o for o in unassessed(s) if o["id"] == p.observation_id), None)
            if not observation:
                return False, "Нужно неоценённое наблюдение текущей карты"
            if not observation["success"] and p.relation != "contradicted":
                return False, "Провал инструмента должен оцениваться как contradicted"
        if name == "REVISE_WORLD":
            required = {o["id"] for o in unresolved_practice(s)}
            if not required.issubset(p.observation_ids) or any(o not in s.observations for o in p.observation_ids):
                return False, "Пересмотр должен учитывать все опровергнутые наблюдения"
        if name == "ASSESS_LEAP":
            roadmap = s.roadmaps[s.current_map]
            if p.resolution_id not in roadmap["resolution_ids"] or s.resolutions[p.resolution_id]["confirmed_roadmap_id"] == s.current_map:
                return False, "Нужно неподтверждённое разрешение текущей карты"
            observations = {o["id"]: o for o in current_observations(s)}
            if any(o not in observations or not observations[o]["success"] for o in p.observation_ids):
                return False, "Оценка скачка опирается на успешные наблюдения текущей карты"
            if not any(s.actions[observations[o]["action_id"]]["tool"] == "commit_card" for o in p.observation_ids):
                return False, "Оценка скачка требует успешной проверки карточки"
        return True, ""


def commit(move: Move, state: State) -> State:
    """Validate first; mutate a deep copy and return only after the whole commit succeeds."""
    ok, error = StructuralValidator.validate(move, state)
    if not ok:
        raise ValueError(error)
    s, p, name = copy.deepcopy(state), move.typed(), move.move
    if name == "PROPOSE_SIMPLEST":
        s.candidate = s.process(p.content)
        did = s.ident("N")
        s.designations[did] = {"id": did, "process_id": s.candidate, "role": "candidate_simplest"}
    elif name == "ASSESS_SIMPLEST":
        pid = s.candidate
        if p.accept:
            s.simplest = pid
            s.processes[pid]["side"] = "simplest"
            for designation in s.designations.values():
                if designation["process_id"] == pid:
                    designation["role"] = "simplest"
        else:
            del s.processes[pid]
            s.designations = {k: d for k, d in s.designations.items() if d["process_id"] != pid}
        s.candidate = None
    elif name == "DEVELOP_PROCESS":
        side = s.processes[p.source_process_id]["side"]
        pid, did = s.process(p.concretization, side), s.ident("D")
        s.developments[did] = {"id": did, **p.model_dump(), "emergent_process_id": pid, "side": side}
        if side == "simplest" and not s.opposite:
            s.opposite = s.process(OPPOSITE, "opposite")
            did = s.ident("N")
            s.designations[did] = {"id": did, "process_id": s.opposite, "role": "opposite"}
    elif name == "ESTABLISH_CONTRADICTION":
        cid = s.ident("C")
        s.contradictions[cid] = {"id": cid, **p.model_dump(), "simplest_id": s.simplest, "opposite_id": s.opposite, "status": "open"}
    elif name == "PROPOSE_LEAP":
        pid, rid = s.process(p.content), s.ident("R")
        qids = []
        for q in p.questions:
            qid = s.ident("Q")
            s.questions[qid] = Question(question_id=qid, text=q.text, field=q.field, why=q.why,
                contradiction_id=p.contradiction_id, points_at_stake=s.req.field_weights.get(q.field, 0))
            qids.append(qid)
        s.resolutions[rid] = {"id": rid, "process_id": pid, "contradiction_id": p.contradiction_id,
            "content": p.content, "outcome": p.outcome, "confirmed_roadmap_id": None, "questions": qids}
    elif name == "BEGIN_EXECUTION":
        mid = s.ident("M")
        s.roadmaps[mid] = {"id": mid, "resolution_ids": p.resolution_ids,
            "contradiction_ids": [s.resolutions[r]["contradiction_id"] for r in p.resolution_ids],
            "execution_process_ids": [s.resolutions[r]["process_id"] for r in p.resolution_ids],
            "question_ids": [q for r in p.resolution_ids for q in s.resolutions[r]["questions"]],
            "route": ["ask_business", "commit_card"] if s.rounds < 2 else ["commit_card"],
            "revision_reason": s.revision_reason,
            "frozen_copy": {"processes": copy.deepcopy(s.processes), "developments": copy.deepcopy(s.developments), "contradictions": copy.deepcopy(s.contradictions)}}
        s.current_map, s.phase = mid, "executing"
    elif name in {"ASK_BUSINESS", "COMMIT_CARD"}:
        aid = s.ident("T")
        s.actions[aid] = {"id": aid, "roadmap_id": s.current_map, "origin_process_id": p.origin_process_id,
            "tool": name.lower(), "args": p.card.model_dump() if name == "COMMIT_CARD" else {"question_ids": s.roadmaps[s.current_map]["question_ids"]},
            "expectation": p.expectation, "status": "pending"}
    elif name == "ASSESS_PRACTICE":
        aid = s.ident("V")
        s.assessments[aid] = {"id": aid, **p.model_dump()}
        if p.relation == "contradicted":
            for cid in s.roadmaps[s.current_map]["contradiction_ids"]:
                s.contradictions[cid]["status"] = "reopened"
    elif name == "REVISE_WORLD":
        s.phase, s.revision_reason = "planning", p.reason
        for rid in s.roadmaps[s.current_map]["resolution_ids"]:
            s.resolutions[rid]["superseded"] = True
    elif name == "ASSESS_LEAP":
        s.resolutions[p.resolution_id]["confirmed_roadmap_id"] = s.current_map
        s.contradictions[s.resolutions[p.resolution_id]["contradiction_id"]]["status"] = "resolved"
    elif name == "COMPLETE":
        s.phase, s.completion = "done", {"reason": p.reason, "roadmap_id": s.current_map}
    return s


def observe(state, action_id, success, payload, error=None, card=None):
    s = copy.deepcopy(state)
    action = s.actions[action_id]
    if action["status"] != "pending":
        raise ValueError("Наблюдение уже зафиксировано")
    oid = s.ident("O")
    s.observations[oid] = {"id": oid, "action_id": action_id, "success": success, "payload": payload, "error": error}
    action["status"] = "success" if success else "failed"
    if action["tool"] == "ask_business" and success:
        s.rounds += 1
        for answer in payload["answers"]:
            aid = s.ident("A")
            s.answers[aid] = {"id": aid, **answer}
            s.sources[aid] = answer["text"]
    if action["tool"] == "commit_card":
        if success:
            s.card = card
        else:
            s.provenance_failures += 1
    return s
