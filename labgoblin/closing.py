"""Owned delivery evidence for a closing response, not a scientific record."""

import hashlib
from pathlib import Path
import uuid

from labgoblin.protocol import integer, table
from labgoblin.storage import digest, hash_file, publish_bytes, publish_json, read_json


VERSION = 1


def enabled(prompt):
    if "closure_response_version" not in prompt:
        return False
    version = prompt["closure_response_version"]
    if type(version) is not int or version != VERSION:
        raise ValueError("Unsupported frozen closing-response capability version")
    return True


class Response:
    def __init__(self, envelope, index, researcher, critique):
        self.root = Path(envelope.root)
        self.directory = self.root / "closure" / str(index)
        self.value = {"version": VERSION, "run_id": envelope.id, "envelope_digest": envelope.digest,
                      "review": index, "researcher_session_id": researcher.id,
                      "prompt": researcher.turns + 1, "critique": critique}
        publish_json(self.directory / "request.json", self.value)

    def submitted(self, number, prompt):
        if number != self.value["prompt"]:
            raise ValueError("Closure response belongs to a different researcher prompt")
        body = prompt.encode("utf-8")
        publish_bytes(self.directory / "prompt.txt", body)
        publish_json(self.directory / "submitted.json", {
            "request_sha256": digest(self.value), "prompt_sha256": hashlib.sha256(body).hexdigest()})

    def responded(self, answer):
        number = self.value["prompt"]
        settled = read_json(self.root / "sessions" / "researcher" / f"settled-prompt-{number}.json")
        if (settled["complete"] is not True or settled["session_id"] != self.value["researcher_session_id"]
                or settled["role"] != "researcher" or settled["prompts"] != number):
            raise ValueError("Closure response requires its exact settled researcher prompt")
        value = {"request_sha256": digest(self.value),
                 "submitted_sha256": digest(read_json(self.directory / "submitted.json")),
                 "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                 "settled_prompt_sha256": digest(settled)}
        publish_json(self.directory / "response.json", value)
        return value


def response_prompts(envelope, sessions):
    """Validate extra prompt authority, including failed attempts with settled bills."""
    root = Path(envelope.root)
    active = enabled(read_json(root / "prompt.json"))
    judges = {role for role in sessions if role.startswith("adjudicator-")}
    if not active:
        if judges:
            raise ValueError("Fresh adjudication requires its frozen closing-response capability")
        return set()
    if not envelope.config.get("closure_critic") or not envelope.config.get("consultant") or not envelope.config["coach"]:
        raise ValueError("Closing-response capability requires the enabled closure critic")
    researcher = sessions["researcher"]
    prompts, covered_judges = set(), set()
    for role in sessions:
        if not role.startswith("coach-") or role == "coach-0":
            continue
        index = int(role.split("-")[1])
        directory = root / "closure" / str(index)
        judge_role = f"adjudicator-{index}"
        if not (directory / "request.json").exists():
            continue
        request = read_json(directory / "request.json")
        number = integer(request.get("prompt"), "closing response prompt")
        critique = table(request.get("critique"), "closing critique")
        adviser = critique.get("adviser_role")
        if (request.get("version") != VERSION or request.get("run_id") != envelope.id
                or request.get("envelope_digest") != envelope.digest or request.get("review") != index
                or request.get("researcher_session_id") != researcher["session_id"]
                or number < 3 or number in prompts
                or adviser not in sessions or not adviser.startswith("consult-")
                or sessions[role]["prompts"] != 1
                or read_json(root / f"review-{index}-proposal.json").get("decision") != "accept"):
            raise ValueError("Closure response lacks its exact owned proposal and researcher identity")
        adviser_root = root / "sessions" / adviser
        original = read_json(adviser_root / "request.json")
        if (critique != read_json(adviser_root / "response.json") or critique.get("status") != "completed"
                or critique.get("purpose") != "closure_critique" or original.get("purpose") != "closure_critique"
                or original.get("caller") != role
                or original.get("caller_session_id") != str(uuid.uuid5(uuid.UUID(envelope.id), role))
                or not critique.get("artifact_sha256") or not sessions[adviser]["complete"]):
            raise ValueError("Closure response is not bound to its completed independent critique")
        if number <= researcher["prompts"]:
            prompts.add(number)
        if judge_role in judges or (directory / "response.json").exists():
            response = read_json(directory / "response.json")
            submitted = read_json(directory / "submitted.json")
            researcher_root = root / "sessions" / "researcher"
            settled = read_json(researcher_root / f"settled-prompt-{number}.json")
            if (number > researcher["prompts"] or response.get("request_sha256") != digest(request)
                    or submitted.get("request_sha256") != digest(request)
                    or response.get("submitted_sha256") != digest(submitted)
                    or submitted.get("prompt_sha256") != hash_file(directory / "prompt.txt")
                    or response.get("answer_sha256") != hash_file(researcher_root / f"answer-{number}.txt")
                    or response.get("settled_prompt_sha256") != digest(settled)
                    or settled.get("complete") is not True or settled.get("role") != "researcher"
                    or settled.get("session_id") != researcher["session_id"] or settled.get("prompts") != number):
                raise ValueError("Adjudication requires the exact submitted and settled researcher response")
            if judge_role in judges:
                reference = read_json(root / "sessions" / judge_role / "closure-adjudication.json")
                if reference != {"review": index, "response_sha256": digest(response),
                                 "request_sha256": digest(request)}:
                    raise ValueError("Fresh adjudicator is not bound to the closing response")
                covered_judges.add(judge_role)
    if covered_judges != judges:
        raise ValueError("Fresh adjudicator lacks an owned researcher response")
    return prompts
