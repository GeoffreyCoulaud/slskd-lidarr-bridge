"""SABnzbd download-client emulation blueprint (Task 16).

Exposes:
  GET|POST /sabnzbd/api   – dispatch on mode=version|get_config|fullstatus|
                            addfile|queue|history + delete sub-actions
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from slskd_lidarr_bridge.web.nzb import parse_nzb


def create_sabnzbd_blueprint(
    download_service,
    *,
    api_key: str | None,
    categories: list[str],
    complete_dir: str,
) -> Blueprint:
    """Build and return the SABnzbd shim Blueprint.

    Args:
        download_service: implements DownloadService with start/statuses/remove.
        api_key: if set, any mode except ``version`` must supply a matching
                 ``apikey`` query/form param.
        categories: SABnzbd category names returned by get_config.
        complete_dir: download completion directory reported to Lidarr.

    Returns:
        A Flask Blueprint registered at url_prefix="/sabnzbd".
    """
    bp = Blueprint("sabnzbd", __name__, url_prefix="/sabnzbd")

    def _get_param(name: str) -> str | None:
        """Read a parameter from query string or form body."""
        return request.args.get(name) or request.form.get(name) or None

    def _api_key_error():
        """Return an error dict if api_key is set and auth fails; else None."""
        if api_key is None:
            return None
        provided = _get_param("apikey")
        if provided != api_key:
            return {"status": False, "error": "API Key Incorrect"}
        return None

    @bp.route("/api", methods=["GET", "POST"])
    def api():
        mode = _get_param("mode") or ""

        # version is exempt from api_key enforcement
        if mode == "version":
            return jsonify({"version": "4.3.0"})

        # All other modes require a valid api_key (when configured)
        err = _api_key_error()
        if err is not None:
            return jsonify(err)

        if mode == "get_config":
            return jsonify(
                {
                    "config": {
                        "misc": {"complete_dir": complete_dir},
                        "categories": categories,
                    }
                }
            )

        if mode == "fullstatus":
            return jsonify({"status": {}})

        if mode == "addfile":
            file_storage = request.files.get("name")
            nzb_bytes = file_storage.read()
            payload = parse_nzb(nzb_bytes)
            category = request.form.get("cat", "")
            nzo_id = download_service.start(payload, category)
            return jsonify({"status": True, "nzo_ids": [nzo_id]})

        if mode == "queue":
            name_param = request.args.get("name")
            if name_param == "delete":
                value = request.args.get("value", "")
                download_service.remove(value)
                return jsonify({"status": True})

            cat_filter = request.args.get("category")
            all_statuses = download_service.statuses()
            slots = []
            slot_index = 0
            for s in all_statuses:
                if s.state != "downloading":
                    continue
                if cat_filter and s.category != cat_filter:
                    continue
                mb = s.total_bytes / (1024 * 1024)
                mbleft = (s.total_bytes - s.transferred_bytes) / (1024 * 1024)
                slots.append(
                    {
                        "nzo_id": s.nzo_id,
                        "filename": s.title,
                        "status": "Downloading",
                        "mb": mb,
                        "mbleft": mbleft,
                        "percentage": int(s.percent),
                        "cat": s.category,
                        "timeleft": "0:00:00",
                        "index": slot_index,
                    }
                )
                slot_index += 1
            return jsonify({"queue": {"slots": slots, "paused": False, "speed": "0"}})

        if mode == "history":
            name_param = request.args.get("name")
            if name_param == "delete":
                value = request.args.get("value", "")
                download_service.remove(value)
                return jsonify({"status": True})

            cat_filter = request.args.get("category")
            all_statuses = download_service.statuses()
            slots = []
            for s in all_statuses:
                if s.state not in ("completed", "failed"):
                    continue
                if cat_filter and s.category != cat_filter:
                    continue
                status_str = "Completed" if s.state == "completed" else "Failed"
                slots.append(
                    {
                        "nzo_id": s.nzo_id,
                        "name": s.title,
                        "nzb_name": s.title,
                        "status": status_str,
                        "storage": s.storage or "",
                        "category": s.category,
                        "fail_message": s.fail_message or "",
                        "bytes": s.total_bytes,
                    }
                )
            return jsonify({"history": {"slots": slots}})

        return jsonify({"status": False, "error": f"Unknown mode: {mode}"})

    return bp
