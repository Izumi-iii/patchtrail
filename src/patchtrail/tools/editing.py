"""Generate deterministic Git diffs from complete file contents, including EOF markers."""

import difflib


def lf_lines(text):
    # str.splitlines also splits Unicode separators which Git does not treat as newlines.
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def file_diff(path, before, after, *, existed=True, exists=True):
    if before == after and existed == exists:
        return ""
    output = [f"diff --git a/{path} b/{path}\n"]
    if not existed:
        output.append("new file mode 100644\n")
    elif not exists:
        output.append("deleted file mode 100644\n")
    lines = difflib.unified_diff(lf_lines(before), lf_lines(after),
                                 fromfile=f"a/{path}" if existed else "/dev/null",
                                 tofile=f"b/{path}" if exists else "/dev/null")
    for line in lines:
        output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(output)
