"""SABnzbd download-client emulation blueprint (Task 16).

Exposes:
  GET|POST /sabnzbd/api   – dispatch on mode=version|get_config|fullstatus|
                            addfile|queue|history + delete sub-actions
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from slskd_lidarr_bridge.adapters.inbound.nzb import parse_nzb
from slskd_lidarr_bridge.domain.download_service import DownloadService

# The bridge only ever handles music; the SABnzbd shim advertises a single,
# fixed category to Lidarr.
_CATEGORIES: list[str] = ["music"]


def create_sabnzbd_blueprint(
    download_service: DownloadService,
    *,
    complete_dir: str,
) -> Blueprint:
    """Build and return the SABnzbd shim Blueprint.

    Args:
        download_service: implements DownloadService with start/statuses/remove.
        complete_dir: download completion directory reported to Lidarr.

    Returns:
        A Flask Blueprint registered at url_prefix="/sabnzbd".
    """
    bp = Blueprint("sabnzbd", __name__, url_prefix="/sabnzbd")

    def _get_param(name: str) -> str | None:
        """Read a parameter from query string or form body."""
        return request.args.get(name) or request.form.get(name) or None

    @bp.route("/api", methods=["GET", "POST"])
    def api() -> Response:
        mode = _get_param("mode") or ""

        if mode == "version":
            return jsonify({"version": "4.3.0"})

        if mode == "get_config":
            return jsonify(
                {
                    "config": {
                        "misc": {"complete_dir": complete_dir},
                        "categories": _CATEGORIES,
                    }
                }
            )

        if mode == "fullstatus":
            return jsonify({"status": {}})

        if mode == "addfile":
            file_storage = request.files.get("name")
            if file_storage is None:
                return jsonify({"status": False, "error": "no nzb file provided"})
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

            cat_filter = request.args.get("category") or request.args.get("cat")
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

            cat_filter = request.args.get("category") or request.args.get("cat")
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
