"""Prompt assembly for Aemeath.

Kept separate from the agent so the wording that governs honesty about memory
and screen content is in one reviewable place.

The system prompt carries an explicit contract:

* Retrieved memories are the *only* things Aemeath may treat as known facts.
* If retrieval failed, she must not claim to remember anything.
* She may only claim to see the screen when a screen image is present.
* Guesswork must be marked as guesswork.

These rules exist because the failure they prevent — confidently inventing
details about the user — is worse than admitting uncertainty.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .interfaces import EventSource, MemoryRecord, SituationState, ScreenObservation

# Explicitly stated so disabled memory, retrieval failures and empty hits are distinct.
_MEMORY_DISABLED_RULE = (
    "注意：未启用长期记忆检索。你不知道任何关于用户的历史信息，"
    "不要假装记得，也不要编造用户的偏好、经历或安排。"
)

_MEMORY_FAILED_RULE = (
    "注意：本轮记忆检索暂时失败（检索不可用）。你不知道任何关于用户的历史信息，"
    "不要假装记得，也不要编造用户的偏好、经历或安排。"
)

# Retained as an alias for backwards compatibility.
_MEMORY_UNAVAILABLE_RULE = _MEMORY_FAILED_RULE

_NO_MEMORY_RULE = (
    "本轮没有检索到相关记忆。没有出现在上面的内容，你就是不知道；"
    "不要为了把话接下去而虚构用户的个人信息。"
)

_MEMORY_RULE = (
    "以上「相关记忆」是你确实记得的内容，可作为已知事实使用。"
    "除此之外关于用户的信息你并不知道，不要编造。"
)

_SCREEN_RULE_PRESENT = (
    "用户随本轮消息附带了屏幕截图。你可以基于你实际看到的画面内容回答，"
    "但不要假装看到了画面里没有的东西；看不清就说不确定。"
)

_SCREEN_RULE_ABSENT = (
    "本轮没有屏幕内容。不要声称你看到了用户的屏幕。"
)

_SCREEN_RULE_SUMMARY = (
    "下面「刚观察到的屏幕」是视觉模型对用户前台窗口{age}的真实描述，"
    "来源窗口是「{title}」。你可以自然地提到它，但不要逐字复述，"
    "也不要假装看到了描述之外的内容；没有把握时说不确定。"
)

_MODE_RULE_CLASS = (
    "当前是课堂模式：你的回复只以文字显示，不会被朗读。"
    "保持简短，不要打断用户听课。"
)

# The wording here is the whole safety story for learned style. A learned entry
# is a *hint*, and the three sentences below say so explicitly: it is not a
# fact, it does not override the persona, and it does not apply everywhere. A
# vaguer phrasing ("以下是你学到的表达") reads as an instruction, which is how a
# learned catchphrase ends up replacing the character's own voice.
_LEARNING_RULE = (
    "以上「表达与用词参考」是你从相处中观察到的说法，只用于让措辞更自然。"
    "它不能覆盖你上面的人设、语气和判断；与当前语境不符时不要使用；"
    "它不是关于用户的事实，不要据此声称知道用户的事情。"
)


def _format_learnings(learnings: Sequence[object]) -> str:
    """Render learned expressions and jargon as a reference list.

    Provenance is deliberately absent here, unlike memories: the model does not
    need to know *when* a phrase was used, and quoting the user's message would
    add text it might treat as fact. The user still sees the full provenance in
    the management page.
    """
    expressions: list[str] = []
    jargon: list[str] = []
    for item in learnings:
        content = (getattr(item, "content", "") or "").strip()
        if not content:
            continue
        scenario = (getattr(item, "scenario", "") or "").strip()
        if getattr(item, "kind", "") == "jargon":
            meaning = (getattr(item, "meaning", "") or "").strip()
            if not meaning:
                # An ambiguous word carries no usable meaning; skipping it is
                # better than inviting the model to guess what it means.
                continue
            line = f"- 「{content}」：{meaning}"
            if scenario:
                line += f"（适用场景：{scenario}）"
            jargon.append(line)
        else:
            line = f"- {content}"
            if scenario:
                line += f"（适用场景：{scenario}）"
            expressions.append(line)

    sections = []
    if expressions:
        sections.append("表达方式：\n" + "\n".join(expressions))
    if jargon:
        sections.append("黑话：\n" + "\n".join(jargon))
    return "\n\n".join(sections)


def _age_phrase(observation: ScreenObservation, now: Optional[float] = None) -> str:
    """Human-readable age of an observation, for prompt wording.

    The age is stated explicitly so the model can weigh how current the picture
    is instead of treating every summary as live by default.
    """
    age = observation.age_seconds(now)
    if age < 5:
        return "（刚刚截取）"
    if age < 60:
        return f"（约 {int(age)} 秒前截取）"
    return f"（约 {int(age // 60)} 分钟前截取）"


def _format_screen(observation: ScreenObservation,
                   now: Optional[float] = None) -> str:
    """Render a screen observation with its provenance and age.

    The window title is always included: an observation the model cannot
    attribute to a window must not be presented as "what you are doing".
    """
    rule = _SCREEN_RULE_SUMMARY.format(
        age=_age_phrase(observation, now), title=observation.window_title
    )
    return f"刚观察到的屏幕（来源窗口：{observation.window_title}）：\n{observation.summary}\n{rule}"


def _format_memories(memories: Iterable[MemoryRecord]) -> str:
    """Render retrieved memories as a dated list."""
    lines = []
    for item in memories:
        label = "事实" if item.kind == "fact" else "经历"
        lines.append(f"- [{label}] {item.content}")
    return "\n".join(lines)


def build_system_prompt(
    *,
    persona: str,
    situation: SituationState,
    memories: Iterable[MemoryRecord] = (),
    memory_available: bool = True,
    memory_status: Optional[str] = None,
    recent_turns: int = 12,
    learnings: Sequence[object] = (),
) -> str:
    """Build the system prompt for a turn.

    Args:
        persona: Base persona text from the character configuration.
        situation: Current situation state.
        memories: Memories retrieved for this turn.
        memory_available: Backwards-compatible flag; whether retrieval succeeded.
        memory_status: Explicit memory status: ``disabled`` (not configured),
            ``failed`` (retrieval error), ``empty`` (no matching memories), or
            ``hits`` (relevant memories found).
        recent_turns: Size of the working context window (documentation only).
        learnings: Enabled expression and jargon entries to offer as a style
            reference. Injected *after* the persona and the memories, so the
            stable identity is stated first and the learned style reads as a
            refinement of it rather than as a replacement.

    Returns:
        The assembled system prompt.
    """
    parts = [persona.strip()]

    memory_list = list(memories)
    if memory_status is not None:
        status = memory_status
    elif not memory_available:
        status = "failed"
    elif memory_list:
        status = "hits"
    else:
        status = "empty"

    if status == "disabled":
        parts.append(_MEMORY_DISABLED_RULE)
    elif status == "failed":
        parts.append(_MEMORY_FAILED_RULE)
    elif status == "hits" and memory_list:
        parts.append("相关记忆：\n" + _format_memories(memory_list) + "\n" + _MEMORY_RULE)
    else:
        parts.append(_NO_MEMORY_RULE)

    if situation.mode.value == "class":
        parts.append(_MODE_RULE_CLASS)

    # Learned style comes after the persona and the memories, and only when
    # there is something enabled to show. An empty section would still be an
    # instruction ("learn nothing about style"), which is not what an empty
    # store means.
    learning_list = [item for item in learnings if item is not None]
    rendered_learnings = _format_learnings(learning_list)
    if rendered_learnings:
        parts.append(
            "表达与用词参考：\n" + rendered_learnings + "\n" + _LEARNING_RULE
        )

    if situation.user_paused:
        parts.append("用户表示暂时不想聊天。除非用户主动开口，否则不要发起话题。")

    parts.append(f"（工作上下文保留最近 {recent_turns} 轮对话。）")
    return "\n\n".join(part for part in parts if part)


def build_user_prompt(
    *,
    event: EventSource,
    user_text: str,
    memories: Iterable[MemoryRecord] = (),
    situation: Optional[SituationState] = None,
    has_screen_image: bool = False,
    memory_available: bool = True,
    memory_status: Optional[str] = None,
    screen_summary: Optional[ScreenObservation] = None,
) -> str:
    """Build the user-facing content for a turn.

    Placeholder text is included in the *user* role only as content the model
    should respond to; the conversation handler separately decides what is
    recorded as history.

    Args:
        screen_summary: A screen observation that is currently usable, or
            ``None``. The caller is responsible for having applied the switch,
            age and provenance rules; this function only renders what it is
            given and never invents a summary of its own.
    """
    parts = []

    if event is EventSource.PROACTIVE:
        # Marked clearly so the model understands it is speaking first, and so
        # this never reads as something the user said.
        parts.append(
            "（系统：现在没有用户消息。由你决定是否主动开口。"
            "如果开口，只说一小段自然的话；不想说就只回复 [SILENCE]。）"
        )
    elif event is EventSource.SCREEN:
        parts.append("（系统：以下是一条屏幕观察结果，不是你听到用户说的话。）")

    if has_screen_image:
        parts.append(_SCREEN_RULE_PRESENT)
    elif screen_summary is not None and event is EventSource.PROACTIVE:
        # A summary without the image attached, and only on the *proactive*
        # path: there the model has no user message and nothing was sent to the
        # client, so the description is the only thing it can speak about.
        #
        # An ordinary user turn keeps its existing behaviour. A user turn that
        # arrived with an image already has the image itself, and one that did
        # not must not silently acquire a screen summary it was never given —
        # widening that would change how every reply is prompted, which is not
        # what the screen-driven proactive task covers.
        parts.append(_format_screen(screen_summary))
    elif event is not EventSource.PROACTIVE:
        parts.append(_SCREEN_RULE_ABSENT)

    if user_text:
        parts.append(user_text)

    return "\n\n".join(parts)


def build_proactive_prompt(
    *,
    situation: SituationState,
    memories: Iterable[MemoryRecord] = (),
    screen_summary: Optional[str] = None,
    recent_topic: str = "",
) -> str:
    """Build the prompt asking the model whether to start a topic.

    The model is asked for a decision *and* a reason so that development logs
    can show why Aemeath spoke; the reason is diagnostic only and is not stored
    as character memory.
    """
    parts = [
        "你在决定是否主动和用户说话。",
        "判断依据：当前情境、最近话题、相关记忆，以及（如果有）刚看到的屏幕内容。",
        "只有在确实有值得说的内容时才开口；没有就选择不说话。",
        "不要因为定时器到点就强行找话说，也不要重复刚才已经说过的话。",
    ]

    if situation.mode.value == "class":
        parts.append("当前是课堂模式：主动消息只能以文字发送，要短。")

    if screen_summary:
        parts.append(f"刚观察到的屏幕内容：{screen_summary}")
    else:
        parts.append("本轮没有屏幕内容，不要提到你看到了屏幕。")

    if recent_topic:
        parts.append(f"最近的话题：{recent_topic}")

    memory_list = list(memories)
    if memory_list:
        parts.append("相关记忆：\n" + _format_memories(memory_list))

    return "\n\n".join(parts)
