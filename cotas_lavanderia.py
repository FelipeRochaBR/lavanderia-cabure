"""
Script para gerenciar cotas da Lavanderia Caburé
Listar, resetar e ajustar limites de agendamentos por unidade

Uso:
    python3 cotas_lavanderia.py                 # Lista todas as cotas
    python3 cotas_lavanderia.py reset           # Reseta todas as cotas
    python3 cotas_lavanderia.py reset <unidade> # Reseta cota de uma unidade
    python3 cotas_lavanderia.py set <unidade> <limite>  # Ajusta limite
    python3 cotas_lavanderia.py historico       # Abre histórico em CSV
"""
import json
import os
import sys
import csv
from datetime import date

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_COTAS = os.path.join(PASTA, 'cotas_lavanderia.json')
ARQUIVO_HISTORICO = os.path.join(PASTA, 'historico_lavanderia.csv')
TOTAL_UNIDADES = 32


def carregar_cotas():
    if os.path.exists(ARQUIVO_COTAS):
        with open(ARQUIVO_COTAS) as f:
            return json.load(f)
    return {}


def salvar_cotas(cotas):
    with open(ARQUIVO_COTAS, 'w') as f:
        json.dump(cotas, f, indent=2, sort_keys=True)
    os.chmod(ARQUIVO_COTAS, 0o600)


def semana_ano_atual():
    hoje = date.today()
    ano = hoje.isocalendar()[0]
    semana = hoje.isocalendar()[1]
    return f"{ano}-W{semana:02d}"


def listar_cotas():
    cotas = carregar_cotas()
    print("\n📊 COTAS DA LAVANDERIA CABURÉ")
    print("=" * 70)
    print(f"{'Unidade':<15} {'Usos':<10} {'Limite':<10} {'Restante':<10} {'Semana':<15}")
    print("-" * 70)

    for unidade in sorted(cotas.keys()):
        dados = cotas[unidade]
        usos = dados.get('usos', 0)
        limite = dados.get('limite_semanal', 3)
        restante = max(0, limite - usos)
        semana = dados.get('semana_atual', '?')
        print(f"{unidade:<15} {usos:<10} {limite:<10} {restante:<10} {semana:<15}")

    print("-" * 70)
    print(f"Total de unidades: {len(cotas)}")
    print(f"Semana atual: {semana_ano_atual()}")


def resetar_todas():
    cotas = carregar_cotas()
    semana = semana_ano_atual()

    print(f"\n⚠️  Resetando TODAS as cotas para a semana {semana}")
    confirm = input("Confirma? (s/n): ").lower()

    if confirm != 's':
        print("❌ Cancelado")
        return

    for unidade in cotas:
        cotas[unidade]['semana_atual'] = semana
        cotas[unidade]['usos'] = 0

    salvar_cotas(cotas)
    print(f"✓ {len(cotas)} cotas resetadas")


def resetar_unidade(unidade):
    cotas = carregar_cotas()

    if unidade not in cotas:
        print(f"❌ Unidade {unidade} não encontrada")
        return

    semana = semana_ano_atual()
    cotas[unidade]['semana_atual'] = semana
    cotas[unidade]['usos'] = 0

    salvar_cotas(cotas)
    print(f"✓ Cota de {unidade} resetada para semana {semana}")


def ajustar_limite(unidade, novo_limite):
    cotas = carregar_cotas()

    if unidade not in cotas:
        print(f"❌ Unidade {unidade} não encontrada")
        return

    try:
        novo_limite = int(novo_limite)
        if novo_limite < 0:
            print(f"❌ Limite deve ser >= 0")
            return
    except ValueError:
        print(f"❌ Limite inválido")
        return

    old_limite = cotas[unidade].get('limite_semanal', 3)
    cotas[unidade]['limite_semanal'] = novo_limite

    salvar_cotas(cotas)
    print(f"✓ Limite de {unidade}: {old_limite} → {novo_limite}")


def abrir_historico():
    if not os.path.exists(ARQUIVO_HISTORICO):
        print(f"❌ Arquivo {ARQUIVO_HISTORICO} não encontrado")
        return

    import subprocess
    try:
        subprocess.run(['libreoffice', '--calc', ARQUIVO_HISTORICO])
    except:
        try:
            subprocess.run(['open', '-a', 'Numbers', ARQUIVO_HISTORICO])
        except:
            print(f"Abra manualmente: {ARQUIVO_HISTORICO}")


def main():
    if len(sys.argv) == 1:
        listar_cotas()
    elif sys.argv[1] == 'reset':
        if len(sys.argv) > 2:
            resetar_unidade(sys.argv[2])
        else:
            resetar_todas()
    elif sys.argv[1] == 'set' and len(sys.argv) > 3:
        ajustar_limite(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == 'historico':
        abrir_historico()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
