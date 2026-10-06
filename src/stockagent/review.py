"""深度报告的反方审稿：撰写完成后，由另一个角色（可路由到另一个模型）专门挑论证的毛病。

现有的核对（未披露、关键事实、数字出处）都是在补全报告；这里反过来找四类问题：
笔记里有的不利事实报告没用上（ignored_evidence）、结论强过证据（overreach）、前后矛盾（inconsistent）、
论断没有依据（unsupported），以及笔记和报告都没覆盖的关键风险（missing_risk）。

模型的意见要经代码把关才有分量：引文必须逐字出现在报告里，依据必须逐字出现在研究笔记里；
没有依据的意见降级。修订只做一轮，且不得引入无出处的数字、不得动程序定价的「12 个月情景」章节；
改不了的意见原样写进附录，不隐藏。
"""

from __future__ import annotations

import re

from . import verify
from .app import App
from .llm import LLMError
from .utils import log, to_simplified

KINDS = ("ignored_evidence", "overreach", "inconsistent", "unsupported", "missing_risk")
FIXABLE = KINDS[:4]
KIND_LABEL = {"ignored_evidence": "笔记里有的不利证据没用上", "overreach": "结论强于证据", "inconsistent": "前后不一致",
              "unsupported": "论断缺少依据", "missing_risk": "遗漏的关键风险"}
SEVERITIES = ("high", "medium", "low")
MAX_OBJECTIONS = 12
BATCH = 6
# 审稿要通读笔记和报告，推理型模型的思考 token 占输出预算的大头（实测三份报告共约 28–29.5k，JSON 本身只有几千）；
# 初始上限给到 48000，避免先在 16000 处被截断、白耗一轮再重试。实际上限仍受 profile.max_tokens 约束
REVIEW_MAX_TOKENS = 48000
SCENARIO_SECTION = "12 个月情景"  # 价格与概率由程序计算，不审、不改
_STRIP = re.compile(r"[\s*_`#>|~\\\-“”‘’『』「」\"']+")

SYSTEM = """你是反方评审。下面给你一份买方深度报告和它依据的研究笔记（事实来源）。你的任务不是润色，而是站在反方，找出这份报告论证上的实质性毛病。
先复述报告的核心判断，再写出反方最强的一版论证（只能用研究笔记里的事实，不得引入笔记里没有的事实）。然后列出具体意见，每条属于下列一类：
- ignored_evidence：研究笔记里有对核心判断不利的事实，报告没提或轻描淡写。必须给出笔记原文（evidence）。
- overreach：报告的措辞或结论强于证据（把推测写成事实、把单一数据点当趋势、把区间一端当基准）。
- inconsistent：报告前后两处说法或数字互相矛盾，或「核心洞见」与「证伪条件」「情景」不一致。必须给出相互矛盾的另一处原文（evidence 填笔记或报告原文）。
- unsupported：关键论断没有任何数据或出处支撑。
- missing_risk：对核心判断很关键、但笔记和报告都没有覆盖的风险或反面情形（quote 留空）。
硬性要求：
- quote 必须是报告里的一整句或一整个表格行，逐字照抄，不要改写、不要缩写；evidence 必须逐字摘自研究笔记。
- 「12 个月情景」章节的价格区间与概率由程序计算，不要对它们提意见；对其文字理由可以。
- 只提实质性问题（会改变读者对核心判断的信心的）：措辞偏好、格式、重复，一律不提。最多 10 条，按严重程度排序。
- severity：high＝可能让核心判断站不住；medium＝削弱某个重要论点；low＝值得注意。
- fix：写出怎么改（补什么事实、降低什么措辞、删掉什么），不要写新的数字。
只输出 JSON。"""

SCHEMA = {"type": "object", "properties": {
    "thesis": {"type": "string"},
    "counter_case": {"type": "string"},
    "objections": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "quote": {"type": "string"}, "evidence": {"type": "string"},
        "problem": {"type": "string"}, "fix": {"type": "string"}},
        "required": ["kind", "severity", "quote", "evidence", "problem", "fix"], "additionalProperties": False}}},
    "required": ["thesis", "counter_case", "objections"], "additionalProperties": False}

PATCH_SYSTEM = """你是这份报告的撰写人，评审提了下面几条意见。逐条决定怎么改，只输出 JSON。
对每条（按编号）：
- 同意改：replacement 写出改后的完整句子或表格行，保持原来的格式和语气；按 fix 的方向补充研究笔记里已有的事实、降低措辞、或去掉站不住的论断。
- 不同意（评审有误，或改不出有依据的版本）：replacement 留空字符串，reason 写原因。
硬性要求：replacement 只能使用研究笔记里已有的数字，不得引入新数字；表格行的列数必须与原行一致；只改这一句或这一行。"""

PATCH_SCHEMA = {"type": "object", "properties": {"patches": {"type": "array", "items": {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "replacement": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["id", "replacement", "reason"], "additionalProperties": False}}},
    "required": ["patches"], "additionalProperties": False}


def norm(text: str) -> str:
    return _STRIP.sub("", to_simplified(text))


def report_units(report: str) -> list[str]:
    """报告里可以整句替换的单位：表格整行，或正文里以句号等结尾的一句。"""
    units: list[str] = []
    for line in report.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("|"):
            units.append(line)
        else:
            units += [x.strip() for x in re.split(r"(?<=[。！？；])", line) if x.strip()]
    return units


def scenario_text(report: str) -> str:
    lines = report.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## ") and SCENARIO_SECTION in l), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def find_unit(report: str, quote: str, units: list[str] | None = None) -> str | None:
    """引文所在的整句或整行（原文写法）；找不到返回 None。引文可以是句子的一部分，但要足够长，避免误中。"""
    q = norm(quote)
    if len(q) < 8:
        return None
    best = None
    for u in units if units is not None else report_units(report):
        nu = norm(u)
        if q in nu:
            if q == nu:
                return u
            if best is None or len(nu) < len(norm(best)):
                best = u
    return best


def vet(data: dict, report: str, notes_text: str) -> tuple[list[dict], int]:
    """给模型的意见定级。返回 (保留的意见, 丢弃数)。
    引文不在报告里的丢弃；依据不在笔记里的：ignored_evidence / inconsistent 丢弃，其余降级（high → medium，missing_risk → low）。"""
    units = report_units(report)
    scen = norm(scenario_text(report))
    notes_n = norm(notes_text) + norm(report)
    out: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for o in data.get("objections", []):
        kind, sev = o.get("kind"), o.get("severity")
        if kind not in KINDS or sev not in SEVERITIES:
            dropped += 1
            continue
        unit = find_unit(report, o.get("quote", ""), units) if o.get("quote", "").strip() else None
        if kind != "missing_risk" and unit is None:
            dropped += 1
            continue
        ev = o.get("evidence", "").strip()
        ev_ok = len(norm(ev)) >= 8 and norm(ev) in notes_n
        if not ev_ok:
            if kind in ("ignored_evidence", "inconsistent"):
                dropped += 1
                continue
            sev = "low" if kind == "missing_risk" else ("medium" if sev == "high" else sev)
        key = norm(unit or o.get("problem", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "severity": sev, "quote": unit or "", "evidence": ev if ev_ok else "",
                    "problem": o.get("problem", "").strip(), "fix": o.get("fix", "").strip(),
                    "in_scenarios": bool(unit and scen and norm(unit) in scen)})
    out.sort(key=lambda x: SEVERITIES.index(x["severity"]))
    return out[:MAX_OBJECTIONS], dropped


def _patchable(o: dict) -> bool:
    return o["kind"] in FIXABLE and o["severity"] in ("high", "medium") and bool(o["quote"]) and not o["in_scenarios"]


def review(app: App, report: str, notes_text: str, sources: list[str]) -> tuple[str, dict]:
    """返回 (修订后的报告, 结果)。结果 = {thesis, counter_case, objections, applied, unresolved, dropped}。
    任何一步模型失败都跳过该步，报告保持原样。"""
    result: dict = {"thesis": "", "counter_case": "", "objections": [], "applied": [], "unresolved": [], "dropped": 0}
    try:
        data = app.llm.complete("review", [SYSTEM], f"研究笔记：\n{notes_text}\n\n报告全文：\n{report}", schema=SCHEMA, max_tokens=REVIEW_MAX_TOKENS).data
    except LLMError as e:
        log.warning("反方审稿失败，跳过：%s", e)
        return report, result
    objections, result["dropped"] = vet(data, report, notes_text)
    result.update(thesis=data.get("thesis", "").strip(), counter_case=data.get("counter_case", "").strip(), objections=objections)

    todo = [o for o in objections if _patchable(o)]
    applied: list[dict] = []
    for start in range(0, len(todo), BATCH):
        part = todo[start:start + BATCH]
        user = (f"研究笔记：\n{notes_text}\n\n报告全文：\n{report}\n\n评审意见：\n"
                + "\n\n".join(f"[{i}]（{KIND_LABEL[o['kind']]}，{o['severity']}）\n原句：{o['quote']}\n问题：{o['problem']}\n"
                              f"建议：{o['fix']}" + (f"\n笔记依据：{o['evidence']}" if o["evidence"] else "")
                              for i, o in enumerate(part)))
        try:
            patches = app.llm.complete("dossier", [PATCH_SYSTEM], user, schema=PATCH_SCHEMA, max_tokens=16000).data["patches"]
        except LLMError as e:
            log.warning("审稿修订第 %d–%d 条失败，跳过：%s", start + 1, start + len(part), e)
            continue
        for p in patches:
            if not 0 <= p["id"] < len(part):
                continue
            o, repl = part[p["id"]], p["replacement"].strip()
            orig = o["quote"]
            if not repl or repl == orig or orig not in report:
                continue
            if orig.startswith("|") and (not repl.startswith("|") or repl.count("|") != orig.count("|")):
                continue  # 表格行的替换必须保持列数
            if not set(verify.check(repl, sources).unverified) <= set(verify.check(orig, sources).unverified):
                continue  # 改写不能引入原句没有的无出处数字
            report = report.replace(orig, repl, 1)
            o["applied"] = True
            applied.append({**o, "replacement": repl, "reason": p.get("reason", "")})
    result["applied"] = applied
    result["unresolved"] = [o for o in objections if not o.get("applied")]
    return report, result


def markdown(result: dict) -> str:
    """附录「审稿意见」。没有任何内容时返回空串。"""
    if not (result["objections"] or result["counter_case"]):
        return ""
    out = ["## § 审稿意见（反方）", "",
           "撰写后由反方评审对照研究笔记挑了一轮论证上的问题。引文与依据都经程序核对过原文；"
           "有依据的意见已尽量改进正文，改不了的列在下面，请自行判断。", ""]
    if result["counter_case"]:
        out += [f"**反方最强论证**：{result['counter_case']}", ""]
    if result["applied"]:
        out += [f"已改写 {len(result['applied'])} 处：", ""]
        for a in result["applied"]:
            out.append(f"- 〔{KIND_LABEL[a['kind']]}·{a['severity']}〕{a['problem']}\n  原文：{a['quote'][:140]}\n  改为：{a['replacement'][:200]}")
        out.append("")
    if result["unresolved"]:
        out += [f"未改动的意见 {len(result['unresolved'])} 条：", ""]
        for o in result["unresolved"]:
            out.append(f"- 〔{KIND_LABEL[o['kind']]}·{o['severity']}〕{o['problem']}"
                       + (f"\n  报告原文：{o['quote'][:140]}" if o["quote"] else "")
                       + (f"\n  笔记依据：{o['evidence'][:160]}" if o["evidence"] else "")
                       + (f"\n  建议：{o['fix'][:160]}" if o["fix"] else ""))
        out.append("")
    if result["dropped"]:
        out.append(f"（另有 {result['dropped']} 条意见因引文或依据在原文里找不到，已被程序丢弃。）")
    return "\n".join(out).rstrip()
