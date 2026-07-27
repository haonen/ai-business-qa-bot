"""
feishu_doc.py — 生成飞书云文档，写入分析报告

需要在飞书开放平台为应用开通以下权限并重新发布版本：
  - docx:document（创建并编辑云文档）
  - sheets:spreadsheet（编辑嵌入电子表格）
  - drive:drive 或 docs:doc（设置链接分享权限）
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import lark_oapi as lark
import requests
from lark_oapi.core.token import TokenManager
from lark_oapi.api.docx.v1 import (
    Block,
    Divider,
    Table,
    TableProperty,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    GetDocumentBlockChildrenRequest,
    Sheet,
    Text,
    TextElement,
    TextElementStyle,
    TextRun,
)
from lark_oapi.api.drive.v1 import (
    PermissionPublicRequest,
    PatchPermissionPublicRequest,
)


# ── Block builders ────────────────────────────────────────────────────────────

def _run(content: str, bold: bool = False, italic: bool = False) -> TextRun:
    style = TextElementStyle.builder()
    if bold:
        style = style.bold(True)
    if italic:
        style = style.italic(True)
    return TextRun.builder().content(str(content)).text_element_style(style.build()).build()


def _make_text(runs: list) -> Text:
    elements = [TextElement.builder().text_run(r).build() for r in runs]
    return Text.builder().elements(elements).build()


def _para(content: str, bold: bool = False, italic: bool = False) -> Block:
    return Block.builder().block_type(2).text(_make_text([_run(content, bold=bold, italic=italic)])).build()


def _heading(content: str) -> Block:
    return Block.builder().block_type(3).heading1(_make_text([_run(content)])).build()


def _heading2(content: str) -> Block:
    return Block.builder().block_type(4).heading2(_make_text([_run(content)])).build()


def _heading3(content: str) -> Block:
    return Block.builder().block_type(5).heading3(_make_text([_run(content)])).build()


def _heading4(content: str) -> Block:
    return Block.builder().block_type(6).heading4(_make_text([_run(content)])).build()


def _bullet(content: str) -> Block:
    return Block.builder().block_type(12).bullet(_make_text([_run(content)])).build()


def _divider() -> Block:
    return Block.builder().block_type(22).divider(Divider.builder().build()).build()


# ── Embedded Sheet creation ──────────────────────────────────────────────────

def _col_label(idx: int) -> str:
    """1-based column index to spreadsheet label: 1 -> A, 27 -> AA."""
    label = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        label = chr(65 + rem) + label
    return label


def _split_sheet_token(token: str) -> tuple[str, str]:
    """
    Docx sheet block returns "spreadsheet_token_sheet_id".
    Spreadsheet tokens may theoretically contain underscores, so split once
    from the right.
    """
    if not token or "_" not in token:
        raise ValueError(f"Unexpected sheet token format: {token}")
    return token.rsplit("_", 1)


def _write_sheet_values(client: lark.Client, spreadsheet_token: str, sheet_id: str, values: list[list[str]]) -> None:
    if not values:
        return

    col_count = max(len(row) for row in values)
    row_count = len(values)
    end_col = _col_label(col_count)
    value_range = f"{sheet_id}!A1:{end_col}{row_count}"

    token = TokenManager.get_self_tenant_token(client.config)
    url = f"{client.config.domain}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"
    resp = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "valueRange": {
                "range": value_range,
                "values": values,
            }
        },
        timeout=client.config.timeout or 30,
    )
    try:
        payload = resp.json()
    except Exception:
        payload = {"code": resp.status_code, "msg": resp.text[:200]}

    if resp.status_code >= 400 or payload.get("code") != 0:
        raise Exception(f"Write sheet values failed [{payload.get('code')}]: {payload.get('msg')}")

    lark.logger.info(
        f"[doc] sheet values written range={payload.get('data', {}).get('updatedRange', value_range)}"
    )


def _compact_wide_table(headers: list, rows: list, max_cols: int = 8) -> tuple[list, list]:
    """
    Embedded doc sheets are more reliable with compact tables. If a table is
    wider than max_cols, merge trailing metric columns instead of falling back
    to pipe text. This keeps every markdown table on the Sheet path.
    """
    if len(headers) <= max_cols:
        return headers, rows

    headers = [str(h) for h in headers]
    rows = [[str(c) for c in row] for row in rows]

    # Common report case: keep both unit and ATV without exceeding width.
    if "件数" in headers and "GMV/件" in headers:
        unit_idx = headers.index("件数")
        atv_idx = headers.index("GMV/件")
        merged_idx = min(unit_idx, atv_idx)
        drop_idx = max(unit_idx, atv_idx)
        new_headers = headers[:]
        new_headers[merged_idx] = "件数/GMV件"
        del new_headers[drop_idx]

        new_rows = []
        for row in rows:
            padded = row + [""] * (len(headers) - len(row))
            merged = f"{padded[unit_idx]} / {padded[atv_idx]}".strip(" /")
            new_row = padded[:]
            new_row[merged_idx] = merged
            del new_row[drop_idx]
            new_rows.append(new_row)
        headers, rows = new_headers, new_rows

    while len(headers) > max_cols:
        merge_start = max_cols - 1
        extra_headers = headers[merge_start:]
        new_rows = []
        for row in rows:
            padded = row + [""] * (len(headers) - len(row))
            parts = [
                f"{h}:{padded[merge_start + idx]}"
                for idx, h in enumerate(extra_headers)
                if padded[merge_start + idx]
            ]
            new_rows.append(padded[:merge_start] + ["; ".join(parts)])
        headers = headers[:merge_start] + ["补充信息"]
        rows = new_rows

    return headers, rows


def _write_sheet_table(client: lark.Client, doc_id: str, headers: list, rows: list):
    """
    Create an embedded spreadsheet block in the doc and write table values into it.

    Returns None on success, or fallback Block objects on failure.
    """
    headers, rows = _compact_wide_table(headers, rows)
    values = [[str(c) for c in headers]]
    values.extend([[str(c) for c in row] for row in rows])
    if not values or not headers:
        return None

    row_count = len(values)
    col_count = max(len(row) for row in values)
    values = [row + [""] * (col_count - len(row)) for row in values]

    # Docx embedded Sheet block creation is sensitive to the visual row_size.
    # Keep the block compact, then write the full data range through Sheets API.
    # Feishu rejects embedded Sheet blocks at row_size=10 with
    # "99992402: field validation failed". row_size=9 is confirmed valid; the
    # Sheets API can still write the full range after the block exists.
    visual_row_count = min(row_count, 9)
    sheet_block = (
        Block.builder()
        .block_type(30)
        .sheet(Sheet.builder().row_size(visual_row_count).column_size(col_count).build())
        .build()
    )

    resp = client.docx.v1.document_block_children.create(
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(doc_id)
        .block_id(doc_id)
        .request_body(
            CreateDocumentBlockChildrenRequestBody.builder().children([sheet_block]).build()
        )
        .build()
    )
    if not resp.success():
        lark.logger.warning(f"[doc] create sheet block failed [{resp.code}]: {resp.msg}")
        return _write_table(client, doc_id, headers, rows) or _table_text_fallback(headers, rows)

    created = (resp.data.children or [None])[0]
    token = getattr(getattr(created, "sheet", None), "token", None)
    if not token:
        lark.logger.warning("[doc] sheet block created without sheet token")
        return _write_table(client, doc_id, headers, rows) or _table_text_fallback(headers, rows)

    try:
        spreadsheet_token, sheet_id = _split_sheet_token(token)
        _write_sheet_values(client, spreadsheet_token, sheet_id, values)
    except Exception as e:
        lark.logger.warning(f"[doc] write sheet values failed ({e})")
        return _write_table(client, doc_id, headers, rows) or _table_text_fallback(headers, rows)

    lark.logger.info(f"[doc] embedded sheet table written ({row_count}x{col_count})")
    return None


# ── Table creation ────────────────────────────────────────────────────────────

def _get_cell_ids_via_get(client: lark.Client, doc_id: str, table_block_id: str) -> list:
    """
    Fallback: fetch table cell block IDs via a GET call.
    Used when the create response doesn't include children.
    """
    all_ids: list = []
    page_token = None
    while True:
        req_b = (
            GetDocumentBlockChildrenRequest.builder()
            .document_id(doc_id)
            .block_id(table_block_id)
            .page_size(200)
        )
        if page_token:
            req_b = req_b.page_token(page_token)
        resp = client.docx.v1.document_block_children.get(req_b.build())
        if not resp.success():
            lark.logger.error(f"[doc] get cells failed: {resp.code} {resp.msg}")
            break
        for item in (resp.data.items or []):
            all_ids.append(item.block_id)
        if not resp.data.has_more:
            break
        page_token = resp.data.page_token
    return all_ids


def _table_text_fallback(headers: list, rows: list) -> list:
    """Pipe-text fallback when native table API fails entirely."""
    result = [_para(" | ".join(str(h) for h in headers), bold=True)]
    result.append(_para("─" * min(60, 10 * len(headers))))
    for row in rows:
        result.append(_para(" | ".join(str(c) for c in row)))
    return result


def _write_table(client: lark.Client, doc_id: str, headers: list, rows: list):
    """
    Create a native Feishu table block and populate each cell.

    Flow:
      1. Create table block → cell block IDs come back in resp.data.children[0].children
      2. If response doesn't include cell IDs, fetch via GET (1 extra call)
      3. For each cell, create a text block as its child (concurrent, 5 threads)

    Returns None on success, or a list of fallback Block objects on failure.
    """
    if not headers:
        return None

    row_count = len(rows) + 1  # header + data rows
    col_count = len(headers)
    all_values = [headers] + rows

    # ── Step 1: Create the table block ───────────────────────────────────────
    prop = (
        TableProperty.builder()
        .row_size(row_count)
        .column_size(col_count)
        .header_row(True)
        .build()
    )
    tbl = Table.builder().property(prop).build()
    tbl_block = Block.builder().block_type(31).table(tbl).build()

    resp = client.docx.v1.document_block_children.create(
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(doc_id)
        .block_id(doc_id)
        .request_body(
            CreateDocumentBlockChildrenRequestBody.builder().children([tbl_block]).build()
        )
        .build()
    )
    if not resp.success():
        lark.logger.warning(f"[doc] create table failed [{resp.code}] — using text fallback")
        return _table_text_fallback(headers, rows)

    table_block = resp.data.children[0]
    table_block_id = table_block.block_id
    lark.logger.info(f"[doc] table created: {table_block_id}")

    # ── Step 2: Get cell block IDs ────────────────────────────────────────────
    # Feishu returns auto-created cell IDs in table_block.children
    cell_ids = list(table_block.children or [])
    if not cell_ids:
        # Older SDK versions may not include children in create response — fall back to GET
        lark.logger.info("[doc] children not in create response, fetching via GET")
        cell_ids = _get_cell_ids_via_get(client, doc_id, table_block_id)

    if not cell_ids:
        lark.logger.warning("[doc] no cell IDs found — using text fallback")
        return _table_text_fallback(headers, rows)

    lark.logger.info(f"[doc] got {len(cell_ids)} cell IDs (expected {row_count * col_count})")

    # ── Step 3: Write text block into each cell (concurrent) ─────────────────
    # Cells have NO default content — we create a new text block in each one.
    errors: list = []

    def write_one(idx: int):
        if idx >= len(cell_ids):
            return
        row_i, col_i = divmod(idx, col_count)
        value = str(all_values[row_i][col_i]) if col_i < len(all_values[row_i]) else ""
        is_header = (row_i == 0)

        text_block = Block.builder().block_type(2).text(
            _make_text([_run(value, bold=is_header)])
        ).build()

        r = client.docx.v1.document_block_children.create(
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(doc_id)
            .block_id(cell_ids[idx])
            .request_body(
                CreateDocumentBlockChildrenRequestBody.builder().children([text_block]).build()
            )
            .build()
        )
        if not r.success():
            errors.append(f"cell {idx}: {r.code}")

    total = min(len(cell_ids), row_count * col_count)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(write_one, i) for i in range(total)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                errors.append(str(e))

    if errors:
        lark.logger.warning(f"[doc] {len(errors)} cell write errors: {errors[:3]}")
    else:
        lark.logger.info(f"[doc] table cells written ({total} cells)")

    return None  # success (even with partial errors, don't fall back the whole table)


# ── Markdown parser ───────────────────────────────────────────────────────────

class _TableSpec:
    """Placeholder for a table to be created via native Feishu table API."""
    def __init__(self, headers: list, rows: list):
        self.headers = headers
        self.rows = rows


def _is_table_separator(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells)


def _looks_like_table_start(lines: list[str], idx: int) -> bool:
    if idx + 1 >= len(lines):
        return False
    current = lines[idx].strip()
    nxt = lines[idx + 1].strip()
    return "|" in current and _is_table_separator(nxt)


def _parse_table_lines(table_lines: list[str]) -> _TableSpec | None:
    headers: list = []
    rows: list = []
    for tl in table_lines:
        line = tl.strip()
        if not line or _is_table_separator(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not headers:
            headers = cells
        else:
            rows.append(cells)
    if headers:
        return _TableSpec(headers, rows)
    return None


def markdown_to_items(content: str) -> list:
    """
    Parse markdown into a list of Block objects and _TableSpec placeholders.
    Tables become _TableSpec so they can be created as native Feishu table blocks.
    """
    items = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        s = lines[i].strip()

        if not s:
            i += 1
            continue

        # Horizontal rule
        if s == "---":
            items.append(_divider())
            i += 1
            continue

        # Markdown headings (deepest level first to avoid prefix conflicts)
        if s.startswith("#### "):
            items.append(_heading4(s[5:].strip()))
            i += 1
            continue
        if s.startswith("### "):
            items.append(_heading3(s[4:].strip()))
            i += 1
            continue
        if s.startswith("## "):
            items.append(_heading2(s[3:].strip()))
            i += 1
            continue
        if s.startswith("# "):
            items.append(_heading(s[2:].strip()))
            i += 1
            continue

        # Markdown table: standard pipe table, including variants without
        # leading/trailing pipes. Every parsed table is written as a Sheet.
        if _looks_like_table_start(lines, i):
            table_lines = []
            while i < len(lines) and "|" in lines[i].strip():
                table_lines.append(lines[i].strip())
                i += 1
            table = _parse_table_lines(table_lines)
            if table:
                items.append(table)
            continue

        # Italic paragraph: _text_
        if s.startswith("_") and s.endswith("_") and len(s) > 2:
            items.append(_para(s[1:-1], italic=True))
            i += 1
            continue

        # Bold heading: **text** (module headers)
        m = re.match(r"^\*\*(.+)\*\*$", s)
        if m and "|" not in s:
            items.append(_heading(m.group(1)))
            i += 1
            continue

        # Bullet: • text
        if s.startswith("•"):
            items.append(_bullet(s[1:].strip()))
            i += 1
            continue

        # Regular paragraph
        items.append(_para(s))
        i += 1

    return items


# ── Block batch writer ────────────────────────────────────────────────────────

def _flush_blocks(client: lark.Client, doc_id: str, blocks: list):
    """Write a batch of simple Block objects to the document root."""
    if not blocks:
        return
    BATCH = 50
    for start in range(0, len(blocks), BATCH):
        batch = blocks[start : start + BATCH]
        wr = client.docx.v1.document_block_children.create(
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(doc_id)
            .block_id(doc_id)
            .request_body(
                CreateDocumentBlockChildrenRequestBody.builder().children(batch).build()
            )
            .build()
        )
        if not wr.success():
            lark.logger.error(f"[doc] flush blocks failed [{wr.code}]: {wr.msg}")


# ── Document creation ─────────────────────────────────────────────────────────

def create_feishu_doc(client: lark.Client, title: str, markdown_content: str) -> str:
    """
    Create a Feishu cloud document and write markdown content into it.

    Text blocks are hand-parsed from Markdown and batch-written.
    Tables are embedded as Sheet blocks, then populated via the Sheets API.

    Returns the document URL (feishu.cn/docx/{doc_id}).

    Required app permissions: docx:document, drive:drive, sheets:spreadsheet
    """
    # Step 1: Create the document
    create_resp = client.docx.v1.document.create(
        CreateDocumentRequest.builder()
        .request_body(CreateDocumentRequestBody.builder().title(title).build())
        .build()
    )
    if not create_resp.success():
        raise Exception(f"Create doc failed [{create_resp.code}]: {create_resp.msg}")

    doc_id = create_resp.data.document.document_id
    lark.logger.info(f"[doc] created doc_id={doc_id}")

    # Step 2: Parse markdown and write content in order.
    items = markdown_to_items(markdown_content)
    pending_blocks: list = []

    for item in items:
        if isinstance(item, _TableSpec):
            _flush_blocks(client, doc_id, pending_blocks)
            pending_blocks = []
            fallback = _write_sheet_table(client, doc_id, item.headers, item.rows)
            if fallback:
                pending_blocks.extend(fallback)
        else:
            pending_blocks.append(item)

    _flush_blocks(client, doc_id, pending_blocks)

    # Step 3: Set org-readable link permission
    try:
        pr = client.drive.v1.permission_public.patch(
            PatchPermissionPublicRequest.builder()
            .token(doc_id)
            .type("docx")
            .request_body(
                PermissionPublicRequest.builder()
                .external_access(True)
                .security_entity("anyone_can_edit")
                .comment_entity("anyone_can_view")
                .share_entity("anyone")
                .link_share_entity("anyone_editable")
                .invite_external(True)
                .build()
            )
            .build()
        )
        if not pr.success():
            lark.logger.warning(f"[doc] set perm failed [{pr.code}]: {pr.msg}")
    except Exception as e:
        lark.logger.warning(f"[doc] set perm error: {e}")

    return f"https://feishu.cn/docx/{doc_id}"
