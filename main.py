from dataclasses import dataclass
from typing import List, Optional, Dict

import uuid
import json

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


fastapi_application = FastAPI()

# 允许前端通过浏览器直接访问
fastapi_application.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    调用哔哩哔哩联想搜索接口。
    只保留 term 以 search_term_prefix 开头的结果，最多十个。
    """
    query_parameters = {
        "term": search_term_prefix,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BilibiliGuessGame/1.0)",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            bilibili_suggestion_endpoint_address,
            params=query_parameters,
            headers=headers,
            timeout=10.0,
        )

    raw_text = response.text
    if not raw_text:
        print("哔哩哔哩返回了空响应。")
        raise HTTPException(status_code=502, detail="哔哩哔哩返回空响应")

    try:
        response_data = response.json()
    except json.JSONDecodeError as problem:
        print("哔哩哔哩响应不是有效的 javascript 对象表示法。原始内容前一千字符：")
        print(raw_text[:1000])
        raise HTTPException(status_code=502, detail="哔哩哔哩返回非 javascript 对象表示法") from problem

    data_section = response_data.get("data")
    if not data_section:
        print("哔哩哔哩响应缺少 data 字段：", response_data)
        return []

    result_section = data_section.get("result")
    if not result_section:
        print("哔哩哔哩响应缺少 result 字段：", response_data)
        return []

    tag_entries = result_section.get("tag", [])
    full_terms: List[str] = [entry.get("term", "") for entry in tag_entries if entry.get("term")]

    filtered_terms: List[str] = []
    for full_term in full_terms:
        if full_term.startswith(search_term_prefix):
            filtered_terms.append(full_term)

    return filtered_terms[:10]


def build_judge_prompt(guess_full_text: str, answer_full_terms: List[str]) -> str:
    """
    构造发送给本地大语言模型的提示文本。
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
    调用本地大语言模型，让其判断猜测是否等价于列表中的某个答案。
    期望模型输出纯 javascript 对象表示法文本。
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
            timeout=60.0,
        )

    try:
        response_data = response.json()
    except json.JSONDecodeError as problem:
        print("大语言模型服务返回的整体响应不是 javascript 对象表示法：", response.text[:1000])
        raise problem

    raw_model_output_text: str = str(response_data.get("response", "")).strip()

    if not raw_model_output_text:
        print("大语言模型返回了空字符串，视为未命中。")
        return {"is_correct": False, "correct_index": -1}

    try:
        judge_result: Dict = json.loads(raw_model_output_text)
    except json.JSONDecodeError:
        print("大语言模型返回的内容不是有效的 javascript 对象表示法：")
        print(raw_model_output_text)
        # 出错时按未命中处理，让游戏继续，而不是直接崩溃
        return {"is_correct": False, "correct_index": -1}

    return judge_result


@fastapi_application.post("/api/start_round", response_model=StartRoundResponse)
async def start_round(request: StartRoundRequest) -> StartRoundResponse:
    """
    创建新的游戏回合。
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
    处理玩家的一次猜测。
    """
    if request.round_identifier not in round_states_by_identifier:
        raise HTTPException(status_code=404, detail="回合不存在")

    round_state: RoundState = round_states_by_identifier[request.round_identifier]

    # 调用大语言模型判断这次猜测是否匹配某个答案
    judge_result: Dict = await judge_guess_with_ollama(
        guess_full_text=request.guess_text,
        answer_full_terms=round_state.answer_full_terms,
    )

    is_correct_from_model: bool = bool(judge_result.get("is_correct", False))
    correct_index_from_model: int = int(judge_result.get("correct_index", -1))

    # 如果索引超出范围，也视为未命中
    if correct_index_from_model < 0 or correct_index_from_model >= len(round_state.answer_full_terms):
        is_correct_from_model = False
        correct_index_from_model = -1

    if not is_correct_from_model:
        # 未命中，增加一次错误
        round_state.strikes += 1
    else:
        # 命中某个索引
        if not round_state.revealed_flags[correct_index_from_model]:
            # 第一次猜中该答案：揭示并加分
            round_state.revealed_flags[correct_index_from_model] = True
            round_state.score += points_by_index[correct_index_from_model]
        else:
            # 重复猜中已经揭示的答案：
            # 不加分、不加错误，也不改变 revealed_flags
            pass

    game_over: bool = round_state.strikes >= round_state.maximum_strikes
    if game_over:
        # 游戏结束时，将所有答案都标记为已揭示
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
