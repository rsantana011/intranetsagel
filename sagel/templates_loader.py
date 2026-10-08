"""Leitura dos fragmentos HTML usados nas páginas."""
from pathlib import Path

def ler_template(nome):
    return (Path(__file__).resolve().parent / 'templates' / nome).read_text(encoding='utf-8')
