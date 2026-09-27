import html
import re
import dkim
import dns.exception
import dns.resolver
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path

IDS = re.compile(r"<[^<>\s]+@[^<>\s]+>")
CODE = re.compile(r"\[mail-oo:([A-Z0-9]{10})\]")
QUOTE = "---- 回复的原邮件 ----"


class BodyHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if self.hidden or "ntes-mailmaster-quote" in classes or attrs.get("id") == "imail_signature" or "ntes-signature" in classes:
            self.hidden += 1
            return
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _decode(part):
    return part.get_content()


def new_body(msg):
    parts = [p for p in msg.walk() if not p.is_multipart() and p.get_content_disposition() != "attachment"]
    plain = next((p for p in parts if p.get_content_type() == "text/plain"), None)
    if plain:
        source = _decode(plain).replace("\r\n", "\n")
        lines = source.splitlines()
        for i, line in enumerate(lines):
            metadata = "\n".join(lines[i+1:i+3])
            markers = sum(bool(re.search(pattern, metadata, re.I)) for pattern in
                          (r"(?:发件人|\bFrom\b)", r"(?:主题|\bSubject\b)", r"(?:日期|\bDate\b|发送时间)"))
            if line.strip() == QUOTE and markers >= 2:
                source = "\n".join(lines[:i])
                break
        else:
            if any(line.strip() == QUOTE for line in lines):
                raise ValueError("无法可靠区分新内容与历史引用")
        return source.strip()
    page = next((p for p in parts if p.get_content_type() == "text/html"), None)
    if page:
        parser = BodyHTML()
        parser.feed(_decode(page))
        return re.sub(r"\n{3,}", "\n\n", html.unescape("".join(parser.parts))).strip()
    raise ValueError("邮件缺少可读取的文本正文")


def parse(raw):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    addresses = getaddresses(msg.get_all("From", []))
    if len(addresses) != 1:
        raise ValueError("发件地址不唯一")
    return msg, addresses[0][1].lower()


def authenticated(msg, sender, raw):
    """Require 163's result and a valid QQ DKIM signature over the message."""
    headers = list(msg.raw_items())
    received = [v for k, v in headers if k.lower() == "received"]
    results = [v for k, v in headers if k.lower() == "authentication-results"]
    if len(results) != 1 or not received:
        return False
    top = re.search(r"\bby\s+([A-Za-z0-9][A-Za-z0-9.-]*)\b", received[0], re.I)
    if not top:
        return False
    result = re.sub(r"\s+", " ", results[0])
    if not re.match(r"^" + re.escape(top.group(1)) + r"\s*;", result, re.I):
        return False
    # Coremail has been observed folding "dkim" as "dki\t m".
    if not (re.search(r"\bdki\s*m\s*=\s*pass\b", result, re.I) and
            re.search(r"\bheader\.i\s*=\s*@qq\.com\b", result, re.I) and
            sender.endswith("@qq.com")):
        return False
    signatures = msg.get_all("DKIM-Signature", [])
    if len(signatures) != 1:
        return False
    signature = str(signatures[0])
    if not re.search(r"(?:^|;)\s*d=qq\.com\s*(?:;|$)", signature, re.I):
        return False
    if not re.search(r"(?:^|;)\s*h=[^;]*\bfrom\b", signature, re.I):
        return False
    return dkim.verify(raw, dnsfunc=dkim_dns)


def dkim_dns(name, timeout=5):
    """Preserve transient DNS failures so the inbox cursor can retry later."""
    try:
        answers = dns.resolver.resolve(name.decode("ascii"), "TXT", lifetime=timeout,
                                       raise_on_no_answer=False, search=False)
    except (dns.exception.Timeout, dns.resolver.NoNameservers,
            dns.resolver.NoResolverConfiguration) as error:
        raise RuntimeError("DKIM DNS 暂时不可用") from error
    except dns.resolver.NXDOMAIN:
        return None
    for answer in answers:
        return b"".join(answer.strings)
    return None


def ids(value):
    return IDS.findall(str(value or ""))


def subject_command(subject):
    s = str(subject).strip()
    m = re.fullmatch(r"(?i:/new)(?: ([^\r\n]+))?", s)
    if m:
        return "new", (m.group(1) or "").strip()
    m = re.fullmatch(r"(?i:/resume) ([A-Z0-9]{10})", s)
    if m:
        return "resume", m.group(1)
    return None, None


def safe_directory(root, relative):
    root = Path(root).expanduser().resolve(strict=True)
    if not relative:
        return root
    if relative.startswith(("/", "~")) or "\\" in relative or any(x in (".", "..", "") for x in relative.split("/")):
        raise ValueError("/new 路径必须是 Documents 下的相对子目录")
    target = (root / relative).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ValueError("工作目录超出 Documents")
    target.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(root):
        raise ValueError("工作目录超出 Documents")
    return target
