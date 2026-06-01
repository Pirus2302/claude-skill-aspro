#!/usr/bin/env python3
"""Достаёт описание эндпоинта(ов) Aspro из openapiru.json.

Зачем: openapiru.json — полная OpenAPI-спека Aspro API (622 эндпоинта,
~4 МБ). Грузить её целиком в контекст дорого и незачем. Этот скрипт находит
нужные пути по подстроке и печатает их операции (методы, параметры, тело
запроса, ответы) с разрешёнными $ref — чтобы НЕ придумывать параметры по
памяти, а смотреть в официальную доку.

Использование (из корня скилла):
    python scripts/lookup_endpoint.py customer_payment
    python scripts/lookup_endpoint.py fin/customer_payment/create
    python scripts/lookup_endpoint.py customfields/fields/list --full

Флаги:
    --full   печатать полностью (без обрезки длинных описаний)
    --list   только список совпавших путей, без тел
"""
import argparse
import json
import sys
from pathlib import Path


def find_spec() -> Path:
    """Ищем openapiru.json в корне скилла (на уровень выше scripts/).

    Поддерживаем два варианта запуска:
    - из корня скилла:        python scripts/lookup_endpoint.py ...
    - из папки scripts/:      python lookup_endpoint.py ...
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "openapiru.json",  # из scripts/ — поднимаемся на уровень
        here / "openapiru.json",         # вдруг лежит рядом
        Path.cwd() / "openapiru.json",   # из текущей рабочей папки
    ]
    for c in candidates:
        if c.is_file():
            return c
    sys.exit(
        "Не нашёл openapiru.json. Запусти из корня скилла "
        "(папки с openapiru.json) или скопируй спеку рядом со скриптом."
    )


def resolve_refs(node, root, depth=0, seen=None):
    """Разворачиваем $ref на несколько уровней, чтобы видеть схемы целиком."""
    if seen is None:
        seen = set()
    if depth > 6:
        return node
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if ref.startswith("#/") and ref not in seen:
                seen = seen | {ref}
                target = root
                for part in ref[2:].split("/"):
                    target = target.get(part, {}) if isinstance(target, dict) else {}
                return resolve_refs(target, root, depth + 1, seen)
            return {"$ref": ref}
        return {k: resolve_refs(v, root, depth + 1, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_refs(x, root, depth + 1, seen) for x in node]
    return node


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="подстрока пути эндпоинта, напр. customer_payment")
    ap.add_argument("--full", action="store_true", help="не обрезать описания")
    ap.add_argument("--list", action="store_true", help="только список путей")
    args = ap.parse_args()

    spec = json.loads(find_spec().read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    q = args.query.lower().strip().lstrip("/")
    matches = {p: ops for p, ops in paths.items() if q in p.lower()}

    if not matches:
        print(f"Ничего не найдено по '{args.query}'.")
        print("Подсказка: ищи по части пути, напр. 'customer_payment', 'customfields/fields'.")
        sys.exit(1)

    print(f"Найдено путей: {len(matches)}\n")
    for p in sorted(matches):
        print(p)
    if args.list:
        return

    print("\n" + "=" * 70)
    for p in sorted(matches):
        ops = resolve_refs(matches[p], spec)
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            print(f"\n### {method.upper()} {p}")
            if op.get("summary"):
                print(f"summary: {op['summary']}")
            desc = op.get("description")
            if desc:
                print(f"description: {desc if args.full else desc[:400]}")
            if op.get("parameters"):
                print("\nparameters:")
                print(json.dumps(op["parameters"], ensure_ascii=False, indent=2))
            if op.get("requestBody"):
                print("\nrequestBody:")
                print(json.dumps(op["requestBody"], ensure_ascii=False, indent=2))
            if op.get("responses"):
                print("\nresponses:")
                print(json.dumps(op["responses"], ensure_ascii=False, indent=2))
            print("-" * 70)


if __name__ == "__main__":
    main()
