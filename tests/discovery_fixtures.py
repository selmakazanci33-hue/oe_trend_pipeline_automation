"""Synthetic dashboard widget requests for discovery tests (invented structures and values)."""

import json

from config.settings import VERIFIED_DASHBOARD_ID, VERIFIED_DASHBOARD_TITLE, VERIFIED_DATASOURCE
from tests.passive_fixtures import PassiveRequest

DASHBOARD_REF = f"{VERIFIED_DASHBOARD_ID};{VERIFIED_DASHBOARD_TITLE}"
FILTER_SENTINEL = "FILTER-MEMBER-SENTINEL"


def widget_payload(widget_id, title, measures=(), dimensions=(), filters=3, widget_type="indicator",
                   dashboard=DASHBOARD_REF, datasource=None, query_guid="GUID-1"):
    metadata = [{"jaql": {"type": "measure", "formula": "Count([AAA-1])", "title": m}} for m in measures]
    for dim in dimensions:
        dim_title, datatype, level = (dim + (None, None))[:3] if isinstance(dim, tuple) else (dim, "text", None)
        jaql = {"dim": f"[t.{dim_title}]", "title": dim_title, "datatype": datatype}
        if level:
            jaql["level"] = level
        metadata.insert(0, {"jaql": jaql})
    for i in range(filters):
        metadata.append({"jaql": {"dim": f"[t.f{i}]", "title": f"f{i}", "isDashboardFilter": i == 0,
                                  "filter": {"members": [FILTER_SENTINEL]}}, "panel": "scope"})
    body = {
        "datasource": dict(datasource or VERIFIED_DATASOURCE),
        "metadata": metadata,
        "widgetType": widget_type,
        "by": "widget",
        "dashboard": dashboard,
        "widget": f"{widget_id};{title}",
        "queryGuid": query_guid,
    }
    return body


def cell(value, text=None):
    return {"data": value, "text": text if text is not None else str(value)}


def rows_text(rows, extra=None):
    body = {"values": rows}
    body.update(extra or {})
    return json.dumps(body)


def widget_request(body, rows, **kw):
    return PassiveRequest(body, text=rows_text(rows), **kw)
