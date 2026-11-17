"""
Limite de usos por semana da Lavanderia Caburé.

Uso:
    python3 cotas_lavanderia.py                         # usos desta semana de cada unidade
    python3 cotas_lavanderia.py limite unidade05 5      # limite próprio para uma unidade
    python3 cotas_lavanderia.py limite unidade05 padrao # volta ao limite padrão
"""
import sys
from datetime import date

from app import (ARQUIVO_AGENDAMENTOS, ARQUIVO_LIMITES, LIMITE_SEMANAL, SEM_LIMITE, ler_json, limite_semanal,
                 salvar_json, semana_do_dia, unidades, usos_na_semana)


def listar():
    agendamentos = ler_json(ARQUIVO_AGENDAMENTOS, {})
    sem = semana_do_dia(date.today())
    print(f'Semana {sem[1]} de {sem[0]} · limite padrão: {LIMITE_SEMANAL} créditos\n')
    print(f"{'Unidade':<12}{'Usos':>6}{'Limite':>8}")
    for u in unidades():
        limite = 'sem' if u in SEM_LIMITE else limite_semanal(u)
        print(f'{u:<12}{usos_na_semana(agendamentos, u, sem):>6}{limite:>8}')


def definir_limite(unidade, valor):
    if unidade not in unidades():
        sys.exit(f'A unidade {unidade} não existe.')
    limites = ler_json(ARQUIVO_LIMITES, {})
    if valor == 'padrao':
        limites.pop(unidade, None)
    elif valor.isdigit():
        limites[unidade] = int(valor)
    else:
        sys.exit('Use um número ou "padrao".')
    salvar_json(ARQUIVO_LIMITES, limites)
    print(f'{unidade}: {limite_semanal(unidade)} créditos por semana.')


if __name__ == '__main__':
    if len(sys.argv) == 1:
        listar()
    elif len(sys.argv) == 4 and sys.argv[1] == 'limite':
        definir_limite(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
