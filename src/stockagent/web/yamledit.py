"""config.yaml / watchlist.yaml / .env 的编辑：保留注释与格式，写入前完整校验，备份后原子写入。

- ruamel.yaml 往返读写，按键原地更新（整块替换会丢掉注释）。
- 股票代码一律写成双引号字符串：运行时用 PyYAML（YAML 1.1）读取，不加引号的 02577 会被当成八进制 1407，
  代码悄悄变成 HK:01407。写入后再按运行时的方式读一遍，与预期不符就拒绝保存。
- 表单带着加载时的文件哈希；保存时文件已被别处修改则拒绝（避免覆盖手工编辑）。
- .env 只改 NAME= 那一行；值不能含换行；页面与日志不显示明文。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from datetime import datetime
from pathlib import Path

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

from ..config import Settings, Watchlist
from ..utils import CST


class EditError(ValueError):
    pass


class Conflict(EditError):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _represent_none(representer, data):
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")  # 原文件写的是 null，不要改成空值


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    y.representer.add_representer(type(None), _represent_none)
    return y


def load(path: Path) -> tuple[CommentedMap, str]:
    text = path.read_text(encoding="utf-8")
    return _yaml().load(text) or CommentedMap(), digest(text)


def dump(data) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def validate_config(text: str) -> Settings:
    try:
        return Settings.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValueError) as e:
        raise EditError(f"配置校验失败：{e}") from e


def validate_watchlist(text: str) -> Watchlist:
    try:
        return Watchlist.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValueError) as e:
        raise EditError(f"观察清单校验失败：{e}") from e


def write(path: Path, text: str, etag: str | None, backup_dir: Path) -> None:
    """备份后原子写入；etag 与磁盘上的当前内容不符时拒绝（文件已被别处修改）。"""
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if etag and digest(current) != etag:
        raise Conflict(f"{path.name} 在你打开页面之后被修改过（可能是手工编辑），请刷新页面后重做修改")
    if current == text:
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    if current:
        (backup_dir / f"{path.name}.{datetime.now(CST):%Y%m%d_%H%M%S}").write_text(current, encoding="utf-8")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def update_map(target: CommentedMap, values: dict, defaults: dict | None = None) -> None:
    """按键原地更新（保留注释）。值为 None：原来有的写成 null（删除键会连带删掉挂在它后面的注释块，
    例如下一节的标题注释），原来没有的不写；原来没有、且等于默认值的键不写入。
    列表沿用原来的写法（原来是 [a, b] 行内格式的仍写成行内格式）。"""
    for k, v in values.items():
        if v is None:
            if k in target and target[k] is not None:
                target[k] = None
            continue
        if k not in target and defaults is not None and defaults.get(k, object()) == v:
            continue
        old = target.get(k)
        if isinstance(v, list) and not isinstance(v, CommentedSeq):
            seq = CommentedSeq(v)
            if isinstance(old, CommentedSeq) and old.fa.flow_style():
                seq.fa.set_flow_style()
            v = seq
        if old == v:
            continue
        target[k] = v


# ---------------------------------------------------------------- 观察清单

def _flow(items: dict) -> CommentedMap:
    m = CommentedMap(items)
    m.fa.set_flow_style()
    return m


def _flow_seq(items: list) -> CommentedSeq:
    s = CommentedSeq(items)
    s.fa.set_flow_style()
    return s


def company_entry(c: dict, existing: CommentedMap | None = None) -> CommentedMap:
    """由表单数据（已经过 Company 校验）生成或原地更新观察清单条目，写法与现有条目一致。"""
    m = existing if existing is not None else CommentedMap()
    listings = CommentedSeq()
    for l in c["listings"]:
        item = {"market": l["market"], "code": DoubleQuotedScalarString(l["code"])}
        if l.get("exchange") and l["market"] != "HK":
            item["exchange"] = l["exchange"]
        if l.get("benchmark"):
            item["benchmark"] = DoubleQuotedScalarString(l["benchmark"])
        if l.get("ads_ratio") and l["market"] == "US":
            r = float(l["ads_ratio"])
            item["ads_ratio"] = int(r) if r.is_integer() else r
        listings.append(_flow(item))
    notes = c.get("notes") or None
    values = {
        "id": c["id"], "name": c["name"], "name_en": c.get("name_en") or None,
        "aliases": _flow_seq(c["aliases"]) if c.get("aliases") else None,
        "listings": listings, "pricing": c.get("pricing") or None, "sector": c.get("sector") or None,
        "notes": LiteralScalarString(notes) if notes and "\n" in notes else notes,
        "watch_items": CommentedSeq(c["watch_items"]) if c.get("watch_items") else None,
        "news_queries": CommentedMap({k: _flow_seq(v) for k, v in c["news_queries"].items() if v}) if c.get("news_queries") else None,
        "twitter_official": _flow_seq(c["twitter_official"]) if c.get("twitter_official") else None,
        "twitter_accounts": _flow_seq(c["twitter_accounts"]) if c.get("twitter_accounts") else None,
    }
    if existing is None:
        for k, v in values.items():
            if v is not None:
                m[k] = v
    else:
        update_map(m, values)
        for k in [k for k, v in values.items() if v is None and k in m]:  # 条目内的可选字段清空时直接删除
            del m[k]
    return m


def check_codes(text: str, expected: dict[str, list[str]]) -> None:
    """按运行时的方式（PyYAML）重读，确认每家公司的上市代码与表单一致（防八进制等隐式类型转换）。"""
    wl = validate_watchlist(text)
    for cid, keys in expected.items():
        c = next((x for x in wl.companies if x.id == cid), None)
        got = [l.key for l in c.listings] if c else []
        if got != keys:
            raise EditError(f"{cid} 的上市代码写入后读出为 {got}，与预期 {keys} 不符，已取消保存")


def upsert_company(path: Path, company: dict, etag: str | None, backup_dir: Path, is_new: bool) -> None:
    data, _ = load(path)
    seq = data.setdefault("companies", CommentedSeq())
    idx = next((i for i, x in enumerate(seq) if x.get("id") == company["id"]), None)
    if is_new and idx is not None:
        raise EditError(f"公司 id「{company['id']}」已存在")
    if not is_new and idx is None:
        raise EditError(f"观察清单中没有「{company['id']}」")
    if idx is None:
        seq.append(company_entry(company))
    else:
        company_entry(company, seq[idx])
    text = dump(data)
    check_codes(text, {company["id"]: [f"{l['market']}:{l['code']}" for l in company["listings"]]})
    write(path, text, etag, backup_dir)


def add_company_handle(path: Path, company_id: str, field: str, handle: str, backup_dir: Path) -> None:
    """把推特账号加到某家公司的 twitter_official / twitter_accounts（保留注释与写法；已有则不重复）。"""
    if field not in ("twitter_official", "twitter_accounts"):
        raise EditError(f"不能写入字段：{field}")
    data, _ = load(path)
    entry = next((x for x in data.get("companies") or [] if x.get("id") == company_id), None)
    if entry is None:
        raise EditError(f"观察清单中没有「{company_id}」")
    seq = entry.get(field)
    if seq is None:
        entry[field] = _flow_seq([handle])
    elif handle.lower() not in [str(x).lower() for x in seq]:
        seq.append(handle)
    text = dump(data)
    validate_watchlist(text)
    write(path, text, None, backup_dir)


def delete_company(path: Path, company_id: str, etag: str | None, backup_dir: Path) -> None:
    data, _ = load(path)
    seq = data.get("companies") or []
    idx = next((i for i, x in enumerate(seq) if x.get("id") == company_id), None)
    if idx is None:
        raise EditError(f"观察清单中没有「{company_id}」")
    if len(seq) == 1:
        raise EditError("观察清单至少要保留一家公司")
    del seq[idx]
    text = dump(data)
    validate_watchlist(text)
    write(path, text, etag, backup_dir)


# ---------------------------------------------------------------- config.yaml

def save_config(path: Path, data: CommentedMap, etag: str | None, backup_dir: Path) -> Settings:
    text = dump(data)
    settings = validate_config(text)
    write(path, text, etag, backup_dir)
    return settings


def save_raw(path: Path, text: str, etag: str | None, backup_dir: Path, kind: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    (validate_config if kind == "config" else validate_watchlist)(text)
    write(path, text, etag, backup_dir)


# ---------------------------------------------------------------- .env

# 只允许密钥类变量名：避免借「设置 key」改写 PATH、PYTHONPATH 之类会影响子进程的环境变量
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*_(KEY|TOKEN|SECRET|WEBHOOK)$")
EXTRA_SECRET_NAMES = {"TWITTER_CT0"}  # twitter-cli 要求的变量名（x.com 的 ct0 Cookie），不以上述后缀结尾；只放行这一个确切的名字


def secret_name_ok(name: str) -> bool:
    return bool(SECRET_NAME_RE.match(name)) or name in EXTRA_SECRET_NAMES


def env_line_re(name: str) -> re.Pattern:
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_\-.:/@+=,]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_env(path: Path, name: str, value: str | None) -> None:
    """设置（value 为 None 时删除）.env 中的一个变量，并同步当前进程的环境变量。"""
    if not secret_name_ok(name):
        raise EditError(f"环境变量名不合法：{name}（只能是大写字母、数字、下划线，并以 _KEY / _TOKEN / _SECRET / _WEBHOOK 结尾）")
    if value is not None and ("\n" in value or "\r" in value):
        raise EditError("值不能包含换行")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pat, out, done = env_line_re(name), [], False
    for line in lines:
        if pat.match(line):
            if value is not None and not done:
                out.append(f"{name}={_quote(value)}")
                done = True
            continue
        out.append(line)
    if value is not None and not done:
        out.append(f"{name}={_quote(value)}")
    tmp = path.with_name(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def mask(value: str | None) -> str:
    if not value:
        return "未设置"
    if len(value) <= 10:
        return f"已设置（{len(value)} 位）"
    return f"{value[:3]}…{value[-4:]}（{len(value)} 位）"
