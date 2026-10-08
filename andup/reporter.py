""""Экспорт результатов поиска дубликатов.

Функции без зависимости от GUI: принимают данные, возвращают
строку или записывают файл по указанному пути. Все диалоги выбора пути
и сообщения - на стороне UI.
"""

import csv
import json
import logging
import os
from datetime import datetime 

from andup.constants import IMG_EXTS, REPORTS_DIR 
from andup.utils import format_size

from andup.constants import DF_VERSION

log = logging.getLogger("df")

# ============================================================================
#                             HTML
# ============================================================================

def build_html_report(duplicates, stats, theme, folder_path="", algorithm="sha256"):
    """Собирает HTML-отчет и возвращает его как строку."""
    parts = ['<!DOCTYPE html><html><head><meta charset="utf-8">', '<title>ANDUP - отчёт</title><style>']
    parts.append(
        f"body{{font-family:Seagoe UI,Arial,sans-serif;background:{theme['bg']};"
        f"color:{theme['fg']};margin:24px}}"
        )
    parts.append( "h1{color:#3498db} h2{color:#2c3e50;border-bottom:1px solid #ccc;padding-bottom:4px;margin-top:32px}" )
    parts.append( "table{border-collapse:collapse;width:100%;margin:10px 0;background:#fff}" )
    parts.append( "th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:13px}" )
    parts.append( "th{background:#ecf0f1}" )
    parts.append( ".img-preview{max-width:100px;max-height:100px;border:1px solid #ccc}" )
    parts.append( ".muted{color:#888;font-size:12px}" )
    parts.append( ".stat{display:inline-block;background:#3498db;color:#fff;padding:8px 14px;border-radius:6px;margin-right:10px;font-weight:bold}" )
    parts.append( "</style></head><body>" )
    
    parts.append( "<h1>Отчёт ANDUP</h1>" )
    parts.append( f"<p class='muted'>Дата: {datetime.now():%Y-%m-%d %H:%M:%S} • " 
                  f"Папка: {folder_path} • Алгоритм: {algorithm.upper()}</p>" )

    total_files = sum(len(v) for v in duplicates.values())
    wasted = stats.get("wasted_size", 0)
    parts.append(
        f"<div>"
        f"<span class='stat'>Групп: {len(duplicates):,}</span>"
        f"<span class='stat'>Файлов: {total_files:,}</span>"
        f"<span class='stat'>Освободить: {format_size(wasted)}</span>"
        f"</div>" )

    for i, (h, files) in enumerate(duplicates.items(), 1):
        parts.append(f"<h2>Группа #{i} • {len(files)} копий</h2>")
        parts.append(f"<p class='muted'>Хеш: {h}</p>")
        parts.append(f"<table><tr><th>#</th><th>Превью</th><th>Путь</th>"
                     "<th>Размер</th><th>Изменен</th></tr>")

        for j, fp in enumerate(files, 1):
            try:
                size = format_size(os.path.getsize(fp))
                mt = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size, mt = "N/A", "N/A"
            ext = os.path.splitext(fp)[1].lower()
            preview = ""
            if ext in IMG_EXTS:
                uri = "file:///" + os.path.abspath(fp).replace("\\", "/")
                preview = f"<img class='img-preview' src='{uri}'>"
            parts.append(
                f"<tr><td>{j}</td><td>{preview}</td>"
                f"<td><a href='file:///{os.path.abspath(fp)}'>{fp}</a></td>"
                f"<td>{size}</td><td>{mt}</td></tr>" )
        parts.append("</table>")

    parts.append("</body></html>")
    return "\n".join(parts)

# ============================================================================
#                             TXT
# ============================================================================
def build_txt_report(duplicates, stats, folder_path="", algorithm="sha256", thread_count=2, use_cache=True, cache_hits=0, cache_misses=0):
    """Собирает текстовый отчёт и возвращает его как строку"""
    total_files = sum(len(fs) for fs in duplicates.values())
    wasted = stats.get("wasted_size", 0)

    lines = []
    lines.append("=" * 85)
    lines.append(" " * 30 + "ОТЧЕТ О ДУБЛИКАТАХ")
    lines.append("=" * 85)
    lines.append("")
    lines.append(f"📅 {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📁 {folder_path}")
    lines.append(f"⚡ {algorithm.upper()} | Потоки: {thread_count}")
    lines.append(f"💾 Кэш: {'Да' if use_cache else 'Нет'} "
                 f"(попаданий: {cache_hits}, промахов: {cache_misses})")
    lines.append(f"🔍 Групп: {len(duplicates):,}")
    lines.append(f"📄 Файлов: {total_files:,}")
    lines.append(f"🗑️ Лишний объем: {format_size(wasted)}")
    lines.append("")
    lines.append("=" * 85)
    lines.append(" " * 32 + "ДЕТАЛЬНО")
    lines.append("=" * 85)
    lines.append("")

    for i, (h, files) in enumerate(duplicates.items(), 1):
        lines.append(f"ГРУППА #{i}")
        lines.append(f"Хеш: {h}")
        lines.append(f"Копий: {len(files)}")
        lines.append("─" * 40)
        for j, fp in enumerate(files, 1):
            try:
                size = format_size(os.path.getsize(fp))
                mt = datetime.fromtimestamp(
                    os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size, mt = "N/A", "N/A"
            lines.append("")
            lines.append(f"{j}. {fp}")
            lines.append(f"   Размер: {size} | Изменён: {mt}")
        lines.append("")
        lines.append("═" * 85)
        lines.append("")

    lines.append("=" * 85)
    lines.append(" " * 32 + "КОНЕЦ ОТЧЕТА")
    lines.append("=" * 85)
    lines.append("")
    return "\n".join(lines)
    

def autosave_report(duplicates, stats, **kwargs):
    """Сохраняет ТХТ-отчёт в REPORTS_DIR с timestamp в имени.
    
    Возвращает путь к файлу.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(REPORTS_DIR, f"duplicates_report_{ts}.txt")
    content = build_txt_report(duplicates, stats, **kwargs)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("Отчёт сохранён: %s", target)
    return target


# ============================================================================
#                             JSON
# ============================================================================    
def write_results_json(duplicates, target_path, stats, folder_path="", algorithm="sha256", thread_count=2, use_cache=True, cache_hits=0, cache_misses=0, version=DF_VERSION):
    """Сохраняет результаты в JSON-файл."""
    data = {
        "metadata": {
            "search_path": folder_path,
            "algorithm": algorithm,
            "thread_count": thread_count,
            "use_cache": use_cache,
            "timestamp": datetime.now().isoformat(),
            "program_version": version,
        },
        "statistics": {
            "total_groups": len(duplicates),
            "total_files": sum(len(f) for f in duplicates.values()),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "wasted_size": stats.get("wasted_size", 0),
        },
        "duplicates": duplicates,
    }
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================================
#                             CSV
# ============================================================================

def write_results_csv(duplicates, target_path):
    """Сохраняет результаты в CSV (разделитель `;`, UTF-8)."""
    with open(target_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Группа", "Хеш", "Номер", "Путь", "Размер", "Изменён"])
        for i, (h, files) in enumerate(duplicates.items(), 1):
            for j, fp in enumerate(files, 1):
                try:
                    size = os.path.getsize(fp)
                    mt = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
                except OSError:
                    size, mt = "N/A", "N/A"
                w.writerow([i, h, j, fp, size, mt])


# ============================================================================
#                          ОДИНОЧНАЯ ГРУППА
# ============================================================================
def write_group_txt(group_num, hash_val, files, target_path):
    """Сохраняет одну группу в ТХТ."""
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(f"ГРУППА #{group_num}\n")
        f.write(f"Хеш: {hash_val}\n")
        f.write(f"Файлов: {len(files)}\n\n")
        for i, fp in enumerate(files, 1):
            try:
                size = format_size(os.path.getsize(fp))
            except OSError:
                size = "N/A"
            f.write(f"{i}. {fp} ({size})\n")


def write_group_json(group_num, hash_val, files, target_path):
    """Сохраняет одну группу в JSON"""
    data = {
        "group_number": group_num,
        "hash": hash_val,
        "file_count": len(files),
        "files": files, 
        "export_time": datetime.now().isoformat(),
    }
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_group_csv(group_num, hash_val, files, target_path):
    """Сохраняет одну группу в CSV"""
    with open(target_path, "w", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Группа", "Хеш", "Номер", "Путь", "Размер", "Изменён"])
        for j, fp in enumerate(files, 1):
            try:
                size = os.path.getsize(fp)
                mt = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size, mt = "N/A", "N/A"
            w.writerow([group_num, hash_val, j, fp, size, mt])


# ============================================================================
#                          ОДИНОЧНАЯ ГРУППА
# ============================================================================
def write_checked_list(paths, target_path):
    """Сохраняет список путей в TXT/JSON/CSV в зависимости от расширения."""
    lower = target_path.lower()
    if lower.endswith(".json"):
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "files": paths,
                    "count": len(paths),
                    "exported_at": datetime.now().isoformat()
                },
                f, ensure_ascii=False, indent=2
            )
    elif lower.endswith(".csv"):
        with open(target_path, "w", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Путь", "Размер", "Изменён"])
            for fp in paths:
                try:
                    size = os.path.getsize(fp)
                    mt = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
                except OSError:
                    size, mt = "N/A", "N/A"
                w.writerow([fp, size, mt])
    else:
        with open(target_path, "w", encoding="utf-8") as f:
            for fp in paths:
                f.write(fp + "\n")