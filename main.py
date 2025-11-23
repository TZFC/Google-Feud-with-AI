from dataclasses import dataclass
from typing import List, Optional, Dict

import uuid
import json

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi.middleware.cors import CORSMiddleware

fastapi_application = FastAPI()
fastapi_application.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # you can restrict later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bilibili_suggestion_endpoint_address: str = "https://api.bilibili.com/x/web-interface/suggest"

ollama_service_endpoint_address: str = "http://localhost:11434/api/generate"
ollama_model_name: str = "autocomplete_judge"


@dataclass
class RoundState:
    search_term_prefix: str
    answer_full_terms: List[str]
    revealed_flags: List[bool]
    score: int
    strikes: int
    maximum_strikes: int


round_states_by_identifier: Dict[str, RoundState] = {}

points_by_index: List[int] = [1000 - index * 100 for index in range(10)]


class StartRoundRequest(BaseModel):
    search_term_prefix: str
    maximum_strikes: int = 5


class StartRoundResponse(BaseModel):
    round_identifier: str
    masked_answers: List[Optional[str]]
    maximum_strikes: int
    search_term_prefix: str


class GuessRequest(BaseModel):
    round_identifier: str
    guess_text: str


class GuessResponse(BaseModel):
    is_correct: bool
    correct_index: int
    revealed_answers: List[Optional[str]]
    score: int
    strikes: int
    game_over: bool
    search_term_prefix: str


async def fetch_bilibili_suggestion_terms(search_term_prefix: str) -> List[str]:
    """
    Call the Bilibili suggestion application programming interface to get
    autocomplete suggestions for the provided search term prefix.

    This function returns only those suggestions whose term begins with the
    provided search term prefix. If the number of suggestions that match
    is fewer than ten, it simply returns the smaller list.
    """
    query_parameters = {
        "term": search_term_prefix,
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            bilibili_suggestion_endpoint_address,
            params=query_parameters,
            timeout=10.0,
        )

    response_data = response.json()

    tag_entries = response_data["data"]["result"]["tag"]
    all_full_terms: List[str] = [entry["term"] for entry in tag_entries]

    filtered_terms: List[str] = []
    for full_term in all_full_terms:
        if full_term.startswith(search_term_prefix):
            filtered_terms.append(full_term)

    return filtered_terms[:10]



def build_judge_prompt(guess_full_text: str, answer_full_terms: List[str]) -> str:
    """
    Build the plain text prompt that will be sent to the large language model judge.
    """
    lines: List[str] = []
    lines.append("Guess:")
    lines.append(guess_full_text)
    lines.append("")
    lines.append("Answers:")
    for index, answer_text in enumerate(answer_full_terms):
        lines.append(f"{index}: {answer_text}")
    lines.append("")
    lines.append("Return JSON only.")
    prompt_text: str = "\n".join(lines)
    return prompt_text


async def judge_guess_with_ollama(guess_full_text: str, answer_full_terms: List[str]) -> Dict:
    """
    Send the guess and answer list to the Ollama model and parse its JSON decision.
    """
    prompt_text: str = build_judge_prompt(
        guess_full_text=guess_full_text,
        answer_full_terms=answer_full_terms,
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            ollama_service_endpoint_address,
            json={
                "model": ollama_model_name,
                "prompt": prompt_text,
                "stream": False,
            },
            timeout=30.0,
        )

    response_data = response.json()
    raw_model_output_text: str = response_data["response"].strip()
    judge_result: Dict = json.loads(raw_model_output_text)

    return judge_result


@fastapi_application.post("/api/start_round", response_model=StartRoundResponse)
async def start_round(request: StartRoundRequest) -> StartRoundResponse:
    """
    Initialize a new game round based on a Bilibili search term prefix.
    """
    answer_full_terms: List[str] = await fetch_bilibili_suggestion_terms(
        search_term_prefix=request.search_term_prefix
    )

    revealed_flags: List[bool] = [False for _ in answer_full_terms]

    round_state = RoundState(
        search_term_prefix=request.search_term_prefix,
        answer_full_terms=answer_full_terms,
        revealed_flags=revealed_flags,
        score=0,
        strikes=0,
        maximum_strikes=request.maximum_strikes,
    )

    round_identifier: str = str(uuid.uuid4())
    round_states_by_identifier[round_identifier] = round_state

    masked_answers: List[Optional[str]] = [None for _ in answer_full_terms]

    return StartRoundResponse(
        round_identifier=round_identifier,
        masked_answers=masked_answers,
        maximum_strikes=request.maximum_strikes,
        search_term_prefix=request.search_term_prefix,
    )


@fastapi_application.post("/api/guess", response_model=GuessResponse)
async def submit_guess(request: GuessRequest) -> GuessResponse:
    """
    Process a player guess for an existing round.
    """
    round_state: RoundState = round_states_by_identifier[request.round_identifier]

    judge_result: Dict = await judge_guess_with_ollama(
        guess_full_text=request.guess_text,
        answer_full_terms=round_state.answer_full_terms,
    )

    is_correct_from_model: bool = bool(judge_result["is_correct"])
    correct_index_from_model: int = int(judge_result["correct_index"])

    if not is_correct_from_model or correct_index_from_model == -1:
        round_state.strikes += 1
    else:
        if not round_state.revealed_flags[correct_index_from_model]:
            round_state.revealed_flags[correct_index_from_model] = True
            round_state.score += points_by_index[correct_index_from_model]

    game_over: bool = round_state.strikes >= round_state.maximum_strikes
    if game_over:
        round_state.revealed_flags = [True for _ in round_state.revealed_flags]

    revealed_answers: List[Optional[str]] = []
    for revealed_flag, answer_text in zip(
        round_state.revealed_flags,
        round_state.answer_full_terms,
    ):
        if revealed_flag:
            revealed_answers.append(answer_text)
        else:
            revealed_answers.append(None)

    is_correct_for_response: bool = is_correct_from_model and correct_index_from_model != -1

    return GuessResponse(
        is_correct=is_correct_for_response,
        correct_index=correct_index_from_model,
        revealed_answers=revealed_answers,
        score=round_state.score,
        strikes=round_state.strikes,
        game_over=game_over,
        search_term_prefix=round_state.search_term_prefix,
    )
