import time
import requests
import gradio as gr

BACKEND = "http://localhost:8000"

current_round_id = None
current_prefix = ""
current_revealed = []
current_score = 0
current_strikes = 0
current_max_strikes = 5
current_guessed_flags = []


def start_round(prefix, maximum_strikes):
    global current_round_id, current_prefix, current_revealed
    global current_score, current_strikes, current_max_strikes, current_guessed_flags

    try:
        resp = requests.post(
            f"{BACKEND}/api/start_round",
            json={"search_term_prefix": prefix, "maximum_strikes": int(maximum_strikes)},
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        return (
            f"启动回合失败: {e}",
            0,
            0,
            "",
            "",
        )

    current_round_id = data["round_identifier"]
    current_prefix = data["search_term_prefix"]
    current_revealed = data["masked_answers"]
    current_score = 0
    current_strikes = 0
    current_max_strikes = data["maximum_strikes"]
    current_guessed_flags = [False] * len(current_revealed)

    status = f"新回合开始！前缀: {current_prefix}；最大失误次数: {current_max_strikes}"

    return (
        status,
        current_score,
        current_strikes,
        render_answers(),
        current_prefix,
    )


def render_answers(game_over=False):
    if not current_revealed:
        return ""

    blocks = []

    for idx, ans in enumerate(current_revealed):
        if ans is None:
            if game_over:
                blocks.append(
                    '<div class="answer-item red">未猜出</div>'
                )
            else:
                blocks.append(
                    '<div class="answer-item gray">???</div>'
                )
        else:
            if game_over:
                if idx < len(current_guessed_flags) and current_guessed_flags[idx]:
                    blocks.append(f'<div class="answer-item green">{ans}</div>')
                else:
                    blocks.append(f'<div class="answer-item red">{ans}</div>')
            else:
                blocks.append(f'<div class="answer-item green">{ans}</div>')

    return "<div class='answer-block'>" + "".join(blocks) + "</div>"


def guess(suffix):
    global current_revealed, current_score, current_strikes
    global current_round_id, current_guessed_flags

    if not current_round_id:
        return "请先开始回合。", current_score, current_strikes, render_answers()

    full_guess = current_prefix + (suffix or "")

    if not full_guess.strip():
        return "请输入猜测内容。", current_score, current_strikes, render_answers()

    try:
        resp = requests.post(
            f"{BACKEND}/api/guess",
            json={"round_identifier": current_round_id, "guess_text": full_guess},
            timeout=20,
        )
        data = resp.json()
    except Exception as e:
        return f"猜测请求失败: {e}", current_score, current_strikes, render_answers()

    if "is_correct" not in data:
        return f"后端返回异常数据: {data}", current_score, current_strikes, render_answers()

    is_correct = data["is_correct"]
    correct_index = data["correct_index"]
    new_score = data["score"]
    new_strikes = data["strikes"]
    game_over = data["game_over"]
    revealed_answers = data["revealed_answers"]

    repeated = False
    if is_correct and 0 <= correct_index < len(revealed_answers):
        if current_guessed_flags[correct_index]:
            repeated = True
        else:
            current_guessed_flags[correct_index] = True

    current_score = new_score
    current_strikes = new_strikes
    current_revealed = revealed_answers

    if game_over:
        return "游戏结束！所有答案已揭示。", current_score, current_strikes, render_answers(game_over=True)

    if repeated:
        return "你已经猜中过这个结果（不加分、不扣次数）。", current_score, current_strikes, render_answers()

    if is_correct:
        msg = f"回答正确！索引 {correct_index} 被成功猜中。"
    else:
        msg = f"回答错误！当前失误次数: {current_strikes}/{current_max_strikes}"

    return msg, current_score, current_strikes, render_answers()


# -------------------------------
# UI + Responsive CSS
# -------------------------------

CSS = """
<style>
.gradio-container {
  max-width: 900px !important;
  margin: 0 auto !important;
  font-size: clamp(15px, 2vw, 20px);
}

.answer-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.answer-item {
  padding: 8px;
  border-radius: 6px;
  font-size: clamp(14px, 3vw, 18px);
}

.answer-item.green { background: #047857; color: #ecfdf5; }
.answer-item.red   { background: #991b1b; color: #fee2e2; }
.answer-item.gray  { background: #374151; color: #d1d5db; }

@media (max-width: 640px) {
  .answer-item { padding: 10px; }
}
</style>
"""


with gr.Blocks(title="自动补全猜词游戏（Gradio 前端）") as demo:

    gr.HTML(CSS)

    gr.Markdown("## 🎮 自动补全猜词游戏（Gradio 版）")

    with gr.Row():
        prefix_input = gr.Textbox(label="输入前缀（例如： 如何 ）", scale=2)
        max_strikes_input = gr.Number(label="最大失误次数", value=5, precision=0, scale=1)
        start_button = gr.Button("开始新回合", variant="primary")

    status_box = gr.Markdown()

    with gr.Row():
        score_box = gr.Number(label="当前得分", value=0, interactive=False, scale=1)
        strike_box = gr.Number(label="当前失误次数", value=0, interactive=False, scale=1)
        prefix_display = gr.Textbox(label="当前前缀", interactive=False, scale=2)

    answers_html = gr.HTML()

    gr.Markdown("### ✏️ 输入猜测（只输入后缀）")
    guess_box = gr.Textbox(label="猜测后缀（例如： 卸载360 ）")
    guess_button = gr.Button("提交猜测", variant="primary")

    start_button.click(
        start_round,
        inputs=[prefix_input, max_strikes_input],
        outputs=[status_box, score_box, strike_box, answers_html, prefix_display],
    )

    guess_button.click(
        guess,
        inputs=[guess_box],
        outputs=[status_box, score_box, strike_box, answers_html],
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    demo.launch(share=args.share)
