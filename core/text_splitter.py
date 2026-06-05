def _force_split(text, max_window):
    if len(text) <= max_window:
        return [text]
    chunks = []
    for i in range(0, len(text), max_window):
        chunks.append(text[i:i + max_window].strip())
    return [c for c in chunks if c]


def split_text(text, sent_window=80, max_window=120, min_chunk=4):
    sentences = text.replace("!", ".").replace("?", ".").replace("\n", ".").split(".")
    sentences = [s.strip() for s in sentences if s.strip()]
    chunks = []
    buf = ""
    for s in sentences:
        if len(s) > max_window:
            if buf and len(buf) >= min_chunk:
                chunks.append(buf)
                buf = ""
            chunks.extend(_force_split(s, max_window))
        elif len(buf) + len(s) + 1 <= max_window:
            buf = (buf + " " + s).strip()
        else:
            if buf and len(buf) >= min_chunk:
                chunks.append(buf)
            buf = s
    if buf and len(buf) >= min_chunk:
        chunks.append(buf)
    if not chunks and text.strip():
        chunks = [text.strip()[:max_window]]
    return chunks
