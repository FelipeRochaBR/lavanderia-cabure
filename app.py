"""Lavanderia Caburé: reserva das máquinas da lavanderia, limite de usos por semana e transparência do uso."""
import csv
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

from flask import Flask, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

PASTA = os.path.dirname(os.path.abspath(__file__))


def carregar_chave_secreta():
    # Quem souber a chave consegue forjar um login, por isso ela fica fora do código
    arquivo = os.path.join(PASTA, 'chave_secreta')
    if not os.path.exists(arquivo):
        with open(arquivo, 'w') as f:
            f.write(secrets.token_hex(32))
        os.chmod(arquivo, 0o600)
    with open(arquivo) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = carregar_chave_secreta()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Mesmos usuários e senhas da Impressão Caburé: trocar a senha em um sistema vale para os dois
PASTA_IMPRESSAO = os.environ.get('PASTA_IMPRESSAO', os.path.join(os.path.dirname(PASTA), 'servidor_impressao'))
ARQUIVO_USUARIOS = os.path.join(PASTA_IMPRESSAO, 'usuarios.json')
# Lista das senhas que a impressão mantém para os gestores; a troca de senha aqui também atualiza
ARQUIVO_SENHAS_TXT = os.path.join(PASTA_IMPRESSAO, 'senhas_unidades.txt')
ARQUIVO_AGENDAMENTOS = os.path.join(PASTA, 'agendamentos.json')
ARQUIVO_HORARIOS = os.path.join(PASTA, 'horarios_disponiveis.json')
# Limite próprio de algumas unidades, ex: {"unidade05": 5}. Ajuste com: python3 cotas_lavanderia.py
ARQUIVO_LIMITES = os.path.join(PASTA, 'cotas_lavanderia.json')
ARQUIVO_DESPESAS = os.path.join(PASTA, 'despesas.json')
ARQUIVO_HISTORICO = os.path.join(PASTA, 'historico_lavanderia.csv')
PASTA_BACKUPS = os.path.join(PASTA, 'backups')
DIAS_BACKUP = 30

MAQUINAS = {'lavadora1': 'Lavadora 1', 'lavadora2': 'Lavadora 2', 'secadora': 'Secadora'}
# Créditos por semana. Cada máquina reservada em um horário gasta 1 crédito (lavar e depois secar = 2)
LIMITE_SEMANAL = 6
# A partir de quantos horários seguidos aparecem as notas sobre água (lavadora) e energia (secadora)
LAVAGENS_SEGUIDAS_AVISO = 3
SECAGENS_SEGUIDAS_AVISO = 3
# Secadora reservada sem lavagem terminando até estas horas antes: pergunta se a pessoa vai só secar
JANELA_LAVAR_SECAR_HORAS = 2
DIAS_AGENDA = 7
SEM_LIMITE = {'fica', 'admin'}  # não têm limite e não entram no rateio
ADMINS = {'admin'}
GESTORES = ADMINS | {'unidade32'}  # lançam despesas e cancelam reservas de qualquer unidade

SENHA_MINIMA = 6
MAX_TENTATIVAS = 5
BLOQUEIO_SEGUNDOS = 5 * 60
tentativas_falhas = {}  # ip -> (erros, horário do último erro)

trava_dados = threading.Lock()
trava_usuarios = threading.Lock()
trava_historico = threading.Lock()
trava_tentativas = threading.Lock()

DIAS_SEMANA = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
NOMES_DIA = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']
NOMES_MES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro',
             'outubro', 'novembro', 'dezembro']


# ===== Dados =====

def ler_json(arquivo, padrao):
    try:
        with open(arquivo) as f:
            return json.load(f)
    except FileNotFoundError:
        return padrao


def salvar_json(arquivo, dados):
    # Grava num arquivo temporário e troca de uma vez, para nunca deixar um JSON pela metade
    temporario = arquivo + '.tmp'
    with open(temporario, 'w') as f:
        json.dump(dados, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.chmod(temporario, 0o600)
    os.replace(temporario, arquivo)


def carregar_usuarios():
    usuarios = ler_json(ARQUIVO_USUARIOS, {})
    for usuario, dados in usuarios.items():
        if isinstance(dados, str):
            usuarios[usuario] = {'senha': dados, 'precisa_trocar_senha': False}
    return usuarios


def unidades():
    return sorted(u for u in carregar_usuarios() if u not in ADMINS)


def registrar_historico(unidade, acao, detalhes):
    with trava_historico:
        novo = not os.path.exists(ARQUIVO_HISTORICO)
        with open(ARQUIVO_HISTORICO, 'a', newline='') as f:
            escritor = csv.writer(f)
            if novo:
                escritor.writerow(['data_hora', 'unidade', 'acao', 'detalhes'])
            escritor.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), unidade, acao, detalhes])


ultimo_backup = [None]


def backup_diario():
    hoje = date.today().isoformat()
    if ultimo_backup[0] == hoje:
        return
    ultimo_backup[0] = hoje
    destino = os.path.join(PASTA_BACKUPS, hoje)
    os.makedirs(destino, exist_ok=True)
    for arquivo in (ARQUIVO_AGENDAMENTOS, ARQUIVO_DESPESAS, ARQUIVO_LIMITES, ARQUIVO_USUARIOS, ARQUIVO_HISTORICO):
        if os.path.exists(arquivo):
            shutil.copy2(arquivo, destino)
    for antigo in sorted(os.listdir(PASTA_BACKUPS))[:-DIAS_BACKUP]:
        shutil.rmtree(os.path.join(PASTA_BACKUPS, antigo), ignore_errors=True)


# ===== Datas e formatação =====

def agora():
    return datetime.now().strftime('%Y-%m-%dT%H:%M')


def mes_atual():
    return date.today().strftime('%Y-%m')


def nome_mes(mes):
    return f'{NOMES_MES[int(mes[5:7]) - 1]} de {mes[:4]}'


def dia_curto(momento):
    dia = date.fromisoformat(momento[:10])
    return f'{NOMES_DIA[dia.weekday()][:3]} {dia.strftime("%d/%m")}'


def horas(minutos):
    h, m = divmod(int(minutos), 60)
    return f'{h}h{m:02d}' if m else f'{h}h'


def reais(valor):
    return 'R$ ' + f'{valor:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def ler_valor_em_reais(texto):
    """'120', '120,50', '120.50' e '1.234,56' -> float. ValueError se não for um valor."""
    texto = re.sub(r'[^0-9.,]', '', texto)
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    return float(texto)


def semana_do_dia(dia):
    return tuple(dia.isocalendar()[:2])


def semana(momento):
    return semana_do_dia(date.fromisoformat(momento[:10]))


def chave_semana(sem):
    return f'{sem[0]}-{sem[1]}'


# ===== Reservas =====

def slots_do_dia(dia, horarios):
    """Horários de um dia como [(inicio, fim)], no formato AAAA-MM-DDTHH:MM."""
    periodos = horarios.get(DIAS_SEMANA[dia.weekday()])
    if not isinstance(periodos, list):
        return []
    slots = []
    for p in periodos:
        if not p.get('ativo', True):
            continue
        inicio = datetime.combine(dia, datetime.strptime(p['inicio'], '%H:%M').time())
        if p['fim'] == '24:00':
            fim = datetime.combine(dia + timedelta(days=1), datetime.min.time())
        else:
            fim = datetime.combine(dia, datetime.strptime(p['fim'], '%H:%M').time())
        passo = timedelta(minutes=p['duracao_slots_minutos'])
        while inicio + passo <= fim:
            slots.append((inicio.strftime('%Y-%m-%dT%H:%M'), (inicio + passo).strftime('%Y-%m-%dT%H:%M')))
            inicio += passo
    return slots


def ocupa_maquina(reserva):
    return not reserva.get('cancelado_em') and not reserva.get('liberado_em')


def ocupacao(agendamentos):
    return {(a['maquina'], a['inicio']): a for a in agendamentos.values() if ocupa_maquina(a)}


def vagas_livres(agendamentos, horarios, momento):
    """Máquinas livres nos próximos dias, do horário mais cedo ao mais tarde: [(maquina, inicio, fim)]."""
    ocupados = ocupacao(agendamentos)
    vagas = []
    for i in range(DIAS_AGENDA):
        for inicio, fim in slots_do_dia(date.today() + timedelta(days=i), horarios):
            if fim > momento:
                vagas.extend((m, inicio, fim) for m in MAQUINAS if (m, inicio) not in ocupados)
    return vagas


def conta_uso(reserva):
    # Cancelar antes do horário devolve o uso; liberar a máquina no meio do horário não devolve
    return not reserva.get('cancelado_em')


def limite_semanal(usuario):
    return ler_json(ARQUIVO_LIMITES, {}).get(usuario, LIMITE_SEMANAL)


def usos_na_semana(agendamentos, usuario, sem):
    return sum(1 for a in agendamentos.values()
               if a['unidade'] == usuario and conta_uso(a) and semana(a['inicio']) == sem)


def horarios_seguidos(agendamentos, usuario, reserva):
    """Quantos horários seguidos, contando o desta reserva, a unidade tem com o mesmo tipo de máquina
    (lavadora ou secadora)."""
    secadora = reserva['maquina'] == 'secadora'
    fim_de = {a['inicio']: a['fim'] for a in agendamentos.values()
              if a['unidade'] == usuario and (a['maquina'] == 'secadora') == secadora and conta_uso(a)}
    inicio_de = {fim: inicio for inicio, fim in fim_de.items()}
    total, depois, antes = 1, reserva['fim'], reserva['inicio']
    while depois in fim_de:
        total, depois = total + 1, fim_de[depois]
    while antes in inicio_de:
        total, antes = total + 1, inicio_de[antes]
    return total


def lavou_antes(agendamentos, usuario, inicio):
    """A unidade tem uma lavagem terminando até JANELA_LAVAR_SECAR_HORAS antes deste horário?"""
    desde = (datetime.fromisoformat(inicio) - timedelta(hours=JANELA_LAVAR_SECAR_HORAS)).strftime('%Y-%m-%dT%H:%M')
    return any(a['unidade'] == usuario and a['maquina'] != 'secadora' and conta_uso(a) and desde <= a['fim'] <= inicio
               for a in agendamentos.values())


def minutos_usados(reserva):
    fim = min(reserva['fim'], reserva.get('liberado_em') or reserva['fim'])
    return max((datetime.fromisoformat(fim) - datetime.fromisoformat(reserva['inicio'])).total_seconds() // 60, 0)


def usos_realizados():
    """Reservas que já começaram e não foram canceladas: é o que entra na transparência e no rateio."""
    momento = agora()
    return [a for a in ler_json(ARQUIVO_AGENDAMENTOS, {}).values() if conta_uso(a) and a['inicio'] <= momento]


def somar_uso(reservas):
    return {'usos': len(reservas),
            'lavadora': sum(1 for a in reservas if a['maquina'] != 'secadora'),
            'secadora': sum(1 for a in reservas if a['maquina'] == 'secadora'),
            'minutos': sum(minutos_usados(a) for a in reservas)}


def calcular_rateio(realizados, todas_unidades):
    """Divide cada despesa entre as unidades pagantes. Devolve (despesas, partes[unidade][mes], pagadoras)."""
    pagadoras = [u for u in todas_unidades if u not in SEM_LIMITE]
    usos_por_mes = {}
    for a in realizados:
        if a['unidade'] in pagadoras:
            usos_por_mes.setdefault(a['inicio'][:7], Counter())[a['unidade']] += 1
    despesas = sorted(ler_json(ARQUIVO_DESPESAS, []), key=lambda d: (d['mes'], d['registrado_em']))
    partes = {u: Counter() for u in pagadoras}
    for d in despesas:
        usos = usos_por_mes.get(d['mes'], Counter())
        d['usos'] = sum(usos.values())
        d['custo_uso'] = d['valor'] / d['usos'] if d['usos'] else 0
        # Proporcional sem nenhum uso no mês: divide igual para a conta não ficar sem dono
        proporcional = d['dividir'] == 'proporcional' and d['usos']
        for u in pagadoras:
            partes[u][d['mes']] += (d['valor'] * usos[u] / d['usos']) if proporcional else d['valor'] / len(pagadoras)
    return despesas, partes, pagadoras


# ===== Login =====

def ip_bloqueado(ip):
    with trava_tentativas:
        erros, quando = tentativas_falhas.get(ip, (0, 0))
        if time.time() - quando > BLOQUEIO_SEGUNDOS:
            tentativas_falhas.pop(ip, None)
            return False
        return erros >= MAX_TENTATIVAS


def registrar_falha(ip):
    with trava_tentativas:
        erros, _ = tentativas_falhas.get(ip, (0, 0))
        tentativas_falhas[ip] = (erros + 1, time.time())


@app.before_request
def exigir_login():
    backup_diario()
    if request.endpoint in ('login', 'static'):
        return None
    if not session.get('usuario'):
        return redirect(url_for('login'))
    if session.get('trocar_senha') and request.endpoint not in ('senha', 'logout'):
        return redirect(url_for('senha'))
    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return pagina('Entrar', HTML_LOGIN)
    ip = request.remote_addr
    if ip_bloqueado(ip):
        return pagina('Entrar', HTML_LOGIN, 429, erro='Muitas tentativas erradas. Espere 5 minutos e tente de novo.')
    usuario = request.form.get('usuario', '').strip().lower()
    dados = carregar_usuarios().get(usuario)
    if not dados or not check_password_hash(dados['senha'], request.form.get('senha', '')):
        registrar_falha(ip)
        return pagina('Entrar', HTML_LOGIN, 401, erro='Usuário ou senha incorretos.')
    with trava_tentativas:
        tentativas_falhas.pop(ip, None)
    session.clear()
    session['usuario'] = usuario
    session['trocar_senha'] = bool(dados.get('precisa_trocar_senha'))
    return redirect(url_for('inicio'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/senha', methods=['GET', 'POST'])
def senha():
    usuario = session['usuario']
    obrigatoria = session.get('trocar_senha', False)
    if request.method == 'GET':
        return pagina('Trocar senha', HTML_SENHA, obrigatoria=obrigatoria, senha_minima=SENHA_MINIMA)
    dados = carregar_usuarios().get(usuario)
    if not dados:
        return redirect(url_for('logout'))
    nova = request.form.get('nova', '')
    erro = None
    if not obrigatoria and not check_password_hash(dados['senha'], request.form.get('atual', '')):
        erro = 'A senha atual está errada.'
    elif len(nova) < SENHA_MINIMA:
        erro = f'A nova senha precisa ter pelo menos {SENHA_MINIMA} caracteres.'
    elif nova != request.form.get('confirmacao', ''):
        erro = 'As duas senhas novas não são iguais.'
    elif check_password_hash(dados['senha'], nova):
        erro = 'A nova senha precisa ser diferente da atual.'
    if erro:
        return pagina('Trocar senha', HTML_SENHA, 400, erro=erro, obrigatoria=obrigatoria, senha_minima=SENHA_MINIMA)
    with trava_usuarios:
        usuarios = carregar_usuarios()
        usuarios[usuario] = {'senha': generate_password_hash(nova), 'precisa_trocar_senha': False}
        salvar_json(ARQUIVO_USUARIOS, usuarios)
        # Mesmo formato do gerar_senhas.py da impressão: troca só a linha desta unidade
        linhas = []
        if os.path.exists(ARQUIVO_SENHAS_TXT):
            with open(ARQUIVO_SENHAS_TXT) as f:
                linhas = [l.rstrip('\n') for l in f if l.strip() and l.split(':')[0] != usuario]
        with open(ARQUIVO_SENHAS_TXT, 'w') as f:
            f.write('\n'.join(sorted(linhas + [f'{usuario}: {nova}'])) + '\n')
        os.chmod(ARQUIVO_SENHAS_TXT, 0o600)
    session['trocar_senha'] = False
    registrar_historico(usuario, 'troca_senha', '')
    return redirect(url_for('inicio', ok='Senha alterada.'))


# ===== Telas do morador =====

@app.route('/')
def inicio():
    usuario = session['usuario']
    agendamentos = ler_json(ARQUIVO_AGENDAMENTOS, {})
    momento = agora()
    meus = sorted((a for a in agendamentos.values()
                   if a['unidade'] == usuario and ocupa_maquina(a) and a['fim'] > momento),
                  key=lambda a: (a['inicio'], a['maquina']))
    limite = limite_semanal(usuario)
    usados = usos_na_semana(agendamentos, usuario, semana(momento))
    saldo = max(limite - usados, 0)

    # "Usar agora": máquinas livres no horário que está rolando, com a secadora do horário seguinte se estiver livre
    vagas = vagas_livres(agendamentos, ler_json(ARQUIVO_HORARIOS, {}), momento)
    livres = {(m, inicio) for m, inicio, _ in vagas}
    agora_livres = [{'maquina': m, 'fim': fim, 'valor': f'{m}|{inicio}',
                     'secar': f'secadora|{fim}' if m != 'secadora' and ('secadora', fim) in livres else None,
                     'so_secar': m == 'secadora' and not lavou_antes(agendamentos, usuario, inicio)}
                    for m, inicio, fim in vagas if inicio <= momento]
    proxima = next(({'maquina': m, 'inicio': inicio} for m, inicio, _ in vagas if inicio > momento), None)

    return pagina('Início', HTML_INICIO, usuario=usuario, meus=meus, momento=momento, maquinas=MAQUINAS,
                  dia_curto=dia_curto, sem_limite=usuario in SEM_LIMITE, limite=limite, usados=usados, saldo=saldo,
                  sem_credito=usuario not in SEM_LIMITE and saldo == 0, agora_livres=agora_livres, proxima=proxima,
                  ok=request.args.get('ok'), erro=request.args.get('erro'), nota_agua=request.args.get('agua') == '1',
                  nota_energia=request.args.get('energia') == '1', texto_agua=TEXTO_AGUA, texto_energia=TEXTO_ENERGIA)


@app.route('/agenda')
def agenda():
    usuario = session['usuario']
    gestor = usuario in GESTORES
    agendamentos = ler_json(ARQUIVO_AGENDAMENTOS, {})
    horarios = ler_json(ARQUIVO_HORARIOS, {})
    momento = agora()
    hoje = date.today()
    ocupados = ocupacao(agendamentos)

    dias, cancelaveis = [], []
    for i in range(DIAS_AGENDA):
        dia = hoje + timedelta(days=i)
        slots = slots_do_dia(dia, horarios)
        linhas, livres = [], 0
        for inicio, fim in slots:
            if fim <= momento:
                continue
            celulas = []
            for maquina in MAQUINAS:
                a = ocupados.get((maquina, inicio))
                if not a:
                    livres += 1
                    celulas.append({'estado': 'livre', 'valor': f'{maquina}|{inicio}', 'maquina': maquina,
                                    'inicio': inicio, 'fim': fim, 'semana': chave_semana(semana(inicio))})
                    continue
                durante = inicio <= momento
                pode_cancelar = (a['unidade'] == usuario or gestor) and fim > momento
                if pode_cancelar:
                    acao = 'Liberar' if durante else 'Cancelar'
                    dono = 'sua reserva' if a['unidade'] == usuario else f"a reserva da {a['unidade']}"
                    cancelaveis.append({'id': a['id'],
                                        'pergunta': f'{acao} {dono} ({MAQUINAS[maquina]}, {inicio[11:]})?'})
                celulas.append({'estado': 'meu' if a['unidade'] == usuario else 'ocupado', 'unidade': a['unidade'],
                                'id': a['id'], 'cancelar': pode_cancelar, 'durante': durante})
            linhas.append({'hora': f'{inicio[11:]}–{fim[11:]}', 'agora': inicio <= momento < fim, 'celulas': celulas})
        if livres:
            situacao = f'{livres} livre' + ('s' if livres > 1 else '')
        elif not slots:
            situacao = 'fechado'
        elif not linhas:
            situacao = 'encerrado'
        else:
            situacao = 'lotado'
        dias.append({'chave': dia.isoformat(), 'titulo': f'{NOMES_DIA[dia.weekday()]}, {dia.strftime("%d/%m")}',
                     'curto': 'Hoje' if i == 0 else f'{NOMES_DIA[dia.weekday()][:3]} {dia.day}',
                     'situacao': situacao, 'aberto': bool(slots), 'linhas': linhas, 'livres': livres})

    # Abre no dia pedido ou no primeiro dia com máquina livre
    chaves = [d['chave'] for d in dias]
    selecionado = request.args.get('dia') if request.args.get('dia') in chaves else \
        next((d['chave'] for d in dias if d['livres']), chaves[0])

    # A agenda pode pegar duas semanas; o limite vale para cada uma
    limite = limite_semanal(usuario)
    semanas = []
    for i in range(DIAS_AGENDA):
        sem = semana_do_dia(hoje + timedelta(days=i))
        if sem not in semanas:
            semanas.append(sem)
    saldos = [{'chave': chave_semana(sem), 'nome': 'esta semana' if n == 0 else 'semana que vem',
               'restam': max(limite - usos_na_semana(agendamentos, usuario, sem), 0)} for n, sem in enumerate(semanas)]
    # A agenda usa isto para não deixar escolher mais máquinas do que os créditos de cada semana
    creditos = None if usuario in SEM_LIMITE else {s['chave']: {'restam': s['restam'], 'nome': s['nome']}
                                                   for s in saldos}
    # Reservas que a unidade já tem: os avisos de sequência e de "só secar" levam em conta o que já foi reservado
    desde = (hoje - timedelta(days=1)).isoformat()
    minhas = [{'maquina': a['maquina'], 'inicio': a['inicio'], 'fim': a['fim']} for a in agendamentos.values()
              if a['unidade'] == usuario and conta_uso(a) and a['inicio'] >= desde]

    return pagina('Agenda', HTML_AGENDA, largo=True, usuario=usuario, dias=dias, selecionado=selecionado,
                  maquinas=MAQUINAS,
                  cancelaveis=cancelaveis, saldos=saldos, creditos=creditos, limite=limite,
                  minhas=minhas, texto_agua=TEXTO_AGUA, texto_energia=TEXTO_ENERGIA,
                  avisos={'lavagens': LAVAGENS_SEGUIDAS_AVISO, 'secagens': SECAGENS_SEGUIDAS_AVISO,
                          'janela_horas': JANELA_LAVAR_SECAR_HORAS},
                  sem_limite=usuario in SEM_LIMITE,
                  ok=request.args.get('ok'), erro=request.args.get('erro'))


@app.route('/agenda', methods=['POST'])
def reservar():
    usuario = session['usuario']
    escolhidos = sorted(set(request.form.getlist('slot')))
    if not escolhidos:
        return redirect(url_for('agenda', erro='Escolha pelo menos uma máquina livre.'))
    horarios = ler_json(ARQUIVO_HORARIOS, {})
    ultimo_dia = date.today() + timedelta(days=DIAS_AGENDA - 1)

    with trava_dados:
        agendamentos = ler_json(ARQUIVO_AGENDAMENTOS, {})
        momento = agora()
        novos = []
        for valor in escolhidos:
            maquina, _, inicio = valor.partition('|')
            try:
                dia = date.fromisoformat(inicio[:10])
            except ValueError:
                dia = None
            fim = dict(slots_do_dia(dia, horarios)).get(inicio) if dia and dia <= ultimo_dia else None
            if maquina not in MAQUINAS or not fim or fim <= momento:
                return redirect(url_for('agenda', erro='Um dos horários escolhidos não está mais disponível.'))
            if any(ocupa_maquina(a) and a['maquina'] == maquina and a['inicio'] == inicio
                   for a in agendamentos.values()):
                return redirect(url_for('agenda', erro=f'{MAQUINAS[maquina]} de {dia_curto(inicio)} às {inicio[11:]} '
                                                       'acabou de ser reservada por outra unidade.'))
            novos.append({'id': secrets.token_hex(6), 'unidade': usuario, 'maquina': maquina,
                          'inicio': inicio, 'fim': fim, 'criado_em': momento})

        if usuario not in SEM_LIMITE:
            limite = limite_semanal(usuario)
            for sem, quantidade in Counter(semana(n['inicio']) for n in novos).items():
                usados = usos_na_semana(agendamentos, usuario, sem)
                if usados + quantidade > limite:
                    return redirect(url_for('agenda', erro=f'São {limite} créditos por semana. Você já usou {usados} '
                                                           f'nessa semana e escolheu mais {quantidade}.'))

        for n in novos:
            agendamentos[n['id']] = n
        salvar_json(ARQUIVO_AGENDAMENTOS, agendamentos)

    for n in novos:
        registrar_historico(usuario, 'reserva', f"{MAQUINAS[n['maquina']]} {n['inicio']}")
    nota_agua = any(n['maquina'] != 'secadora' and
                    horarios_seguidos(agendamentos, usuario, n) >= LAVAGENS_SEGUIDAS_AVISO for n in novos)
    nota_energia = any(n['maquina'] == 'secadora' and
                       horarios_seguidos(agendamentos, usuario, n) >= SECAGENS_SEGUIDAS_AVISO for n in novos)
    return redirect(url_for('inicio', ok='Reserva feita!' if len(novos) == 1 else f'{len(novos)} reservas feitas!',
                            agua=1 if nota_agua else None, energia=1 if nota_energia else None))


@app.route('/reserva/<id_reserva>/cancelar', methods=['POST'])
def cancelar(id_reserva):
    usuario = session['usuario']
    voltar = 'agenda' if request.form.get('voltar') == 'agenda' else 'inicio'
    with trava_dados:
        agendamentos = ler_json(ARQUIVO_AGENDAMENTOS, {})
        reserva = agendamentos.get(id_reserva)
        momento = agora()
        if not reserva or not ocupa_maquina(reserva) or reserva['fim'] <= momento:
            return redirect(url_for(voltar, erro='Essa reserva não existe mais ou já terminou.'))
        if reserva['unidade'] != usuario and usuario not in GESTORES:
            return redirect(url_for(voltar, erro='Você só pode cancelar as suas reservas.'))
        # Antes do horário: cancela e devolve o uso. Durante: libera a máquina pelo resto do horário.
        campo = 'cancelado_em' if reserva['inicio'] > momento else 'liberado_em'
        reserva[campo] = momento
        reserva['cancelado_por'] = usuario
        salvar_json(ARQUIVO_AGENDAMENTOS, agendamentos)

    detalhes = f"{MAQUINAS[reserva['maquina']]} {reserva['inicio']} ({reserva['unidade']})"
    if campo == 'cancelado_em':
        registrar_historico(usuario, 'cancelamento', detalhes)
        return redirect(url_for(voltar, ok='Reserva cancelada. O crédito foi devolvido.'))
    registrar_historico(usuario, 'liberacao', detalhes)
    return redirect(url_for(voltar, ok='Máquina liberada para outras unidades.'))


@app.route('/horarios')
def ver_horarios():
    horarios = ler_json(ARQUIVO_HORARIOS, {})
    dias = []
    for i, chave in enumerate(DIAS_SEMANA):
        periodos = horarios.get(chave)
        ativos = [p for p in periodos if p.get('ativo', True)] if isinstance(periodos, list) else []
        dias.append({'nome': NOMES_DIA[i], 'periodos': ativos})
    return pagina('Horários', HTML_HORARIOS, dias=dias, maquinas=MAQUINAS)


# ===== Transparência (todas as unidades logadas veem) =====

@app.route('/transparencia')
def transparencia():
    realizados = usos_realizados()
    todas = unidades()
    meses = sorted({a['inicio'][:7] for a in realizados} | {mes_atual()}, reverse=True)
    mes = request.args.get('mes') if request.args.get('mes') in meses else mes_atual()
    unidade = request.args.get('unidade') if request.args.get('unidade') in todas else None
    _, partes, _ = calcular_rateio(realizados, todas)
    gasto = {u: sum(partes[u].values()) if u in partes else None for u in todas}

    if unidade:
        realizados = [a for a in realizados if a['unidade'] == unidade]
    do_mes = [a for a in realizados if a['inicio'][:7] == mes]
    por_unidade = {u: {'mes': somar_uso([a for a in do_mes if a['unidade'] == u]),
                       'total': somar_uso([a for a in realizados if a['unidade'] == u])} for u in todas}
    por_maquina = {m: somar_uso([a for a in do_mes if a['maquina'] == m]) for m in MAQUINAS}
    por_mes = [(m, somar_uso([a for a in realizados if a['inicio'][:7] == m]),
                partes[unidade][m] if unidade in partes else None)
               for m in sorted({a['inicio'][:7] for a in realizados}, reverse=True)]

    return pagina('Transparência', HTML_USO, largo=True, aba='uso', meses=meses, mes=mes, unidade=unidade,
                  unidades=todas, por_unidade=por_unidade, por_maquina=por_maquina, por_mes=por_mes,
                  uso_mes=somar_uso(do_mes), lista_mes=sorted(do_mes, key=lambda a: (a['inicio'], a['maquina'])),
                  gasto=gasto, maquinas=MAQUINAS, nome_mes=nome_mes, dia_curto=dia_curto, horas=horas, reais=reais)


@app.route('/transparencia/rateio')
def rateio():
    usuario = session['usuario']
    realizados = usos_realizados()
    despesas, partes, pagadoras = calcular_rateio(realizados, unidades())
    meses = sorted({d['mes'] for d in despesas} | {a['inicio'][:7] for a in realizados} | {mes_atual()},
                   reverse=True)
    mes = request.args.get('mes') if request.args.get('mes') in meses else mes_atual()

    usos_mes = Counter(a['unidade'] for a in realizados if a['inicio'][:7] == mes)
    despesas_mes = [d for d in despesas if d['mes'] == mes]
    total_mes = sum(d['valor'] for d in despesas_mes)
    usos_total_mes = sum(usos_mes[u] for u in pagadoras)
    linhas = [{'unidade': u, 'usos': usos_mes[u], 'mes': partes[u][mes], 'total': sum(partes[u].values())}
              for u in pagadoras]
    resumo = '\n'.join(
        [f'Lavanderia Caburé - rateio de {nome_mes(mes)}',
         f'Despesas: {reais(total_mes)} (' + ', '.join(f"{d['descricao']} {reais(d['valor'])}" for d in despesas_mes) + ')',
         f'Usos no mês: {usos_total_mes}', '']
        + [f"{l['unidade']}: {reais(l['mes'])} ({l['usos']} usos)" for l in linhas])

    return pagina('Transparência', HTML_RATEIO, largo=True, aba='rateio', meses=meses, mes=mes, linhas=linhas,
                  despesas=list(reversed(despesas)), total_mes=total_mes, usos_total_mes=usos_total_mes,
                  total_geral=sum(d['valor'] for d in despesas), pagadoras=pagadoras, resumo=resumo,
                  gestor=usuario in GESTORES, erro=request.args.get('erro'), nome_mes=nome_mes, reais=reais)


@app.route('/transparencia/despesas', methods=['POST'])
def lancar_despesa():
    usuario = session['usuario']
    if usuario not in GESTORES:
        return redirect(url_for('rateio', erro='Só os gestores lançam despesas.'))
    mes = request.form.get('mes', '')
    descricao = request.form.get('descricao', '').strip()[:80]
    try:
        valor = ler_valor_em_reais(request.form.get('valor', ''))
    except ValueError:
        valor = 0
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', mes) or not descricao or valor <= 0:
        return redirect(url_for('rateio', erro='Confira o mês, a descrição e o valor da despesa.') + '#despesas')
    with trava_dados:
        despesas = ler_json(ARQUIVO_DESPESAS, [])
        despesas.append({'id': secrets.token_hex(4), 'mes': mes, 'descricao': descricao, 'valor': round(valor, 2),
                         'dividir': 'igual' if request.form.get('dividir') == 'igual' else 'proporcional',
                         'registrado_por': usuario, 'registrado_em': datetime.now().strftime('%Y-%m-%d %H:%M')})
        salvar_json(ARQUIVO_DESPESAS, despesas)
    registrar_historico(usuario, 'despesa', f'{mes} {descricao} {reais(valor)}')
    return redirect(url_for('rateio', mes=mes) + '#despesas')


@app.route('/transparencia/despesas/<id_despesa>/apagar', methods=['POST'])
def apagar_despesa(id_despesa):
    usuario = session['usuario']
    if usuario not in GESTORES:
        return redirect(url_for('rateio', erro='Só os gestores apagam despesas.'))
    with trava_dados:
        despesas = ler_json(ARQUIVO_DESPESAS, [])
        salvar_json(ARQUIVO_DESPESAS, [d for d in despesas if d['id'] != id_despesa])
    registrar_historico(usuario, 'despesa_apagada', id_despesa)
    return redirect(url_for('rateio', mes=request.form.get('mes')) + '#despesas')


# ===== Layout =====

HTML_BASE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ titulo }} - Lavanderia Caburé</title>
<style>
:root {
    /* Verde da marca FICA (#72BF45), igual à Impressão Caburé */
    --marca: #72bf45; --fundo: #eef6e8; --cartao: #ffffff; --texto: #1f2a1a; --suave: #5f6b58; --borda: #d3dfcb;
    --campo: #f7fbf4; --primaria: #367d1b; --primaria-hover: #2d6a16; --primaria-clara: #e6f3dd;
    --ok: #367d1b; --ok-claro: #e6f3dd; --erro: #c62828; --erro-claro: #fdecec; --aviso: #d97706;
    --meu: #1d4ed8; --meu-claro: #e3ecfb;
}
* { box-sizing: border-box; }
button, input, select { font-family: inherit; }
body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px 16px; background: var(--fundo); color: var(--texto);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 16px; line-height: 1.5;
}
.cartao {
    width: 100%; max-width: 420px; background: var(--cartao); border-radius: 18px; padding: 32px 28px;
    border-top: 6px solid var(--marca);
    box-shadow: 0 1px 2px rgba(16, 24, 40, .06), 0 12px 32px rgba(16, 24, 40, .08);
}
.cartao.largo { max-width: 880px; }
.icone {
    width: 56px; height: 56px; border-radius: 16px; margin: 0 auto 16px; display: flex;
    align-items: center; justify-content: center; background: var(--marca); color: #fff;
}
.icone svg { width: 30px; height: 30px; }
h1 { font-size: 22px; margin: 0 0 4px; text-align: center; }
h2 { font-size: 17px; margin: 0 0 10px; }
.projeto { text-align: center; font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--primaria); margin: 0 0 4px; }
.subtitulo { color: var(--suave); text-align: center; margin: 0 0 24px; }
label.rotulo { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
input[type="text"], input[type="password"] {
    width: 100%; padding: 12px 14px; font-size: 16px; color: var(--texto); background: var(--campo);
    border: 1px solid var(--borda); border-radius: 10px; margin-bottom: 16px; outline: none;
}
input:focus { border-color: var(--primaria); box-shadow: 0 0 0 3px var(--primaria-clara); }
.botao {
    display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; padding: 14px;
    font-size: 16px; font-weight: 600; color: #fff; background: var(--primaria); border: none;
    border-radius: 10px; cursor: pointer; text-decoration: none; transition: background .15s;
}
.botao:hover { background: var(--primaria-hover); }
.alerta, .sucesso { padding: 12px 14px; border-radius: 10px; margin-bottom: 16px; font-size: 15px; }
.alerta { background: var(--erro-claro); color: var(--erro); }
.sucesso { background: var(--ok-claro); color: var(--ok); font-weight: 600; }
.nota { padding: 12px 14px; border-radius: 10px; margin-bottom: 16px; font-size: 15px; background: var(--meu-claro); color: var(--meu); }
.barra-reserva #aviso.agua { background: var(--meu-claro); color: var(--meu); }
.nota.energia, .barra-reserva #aviso.energia { background: #fff4e0; color: #8a4b00; }
.barra-reserva #aviso.pergunta { background: var(--campo); color: var(--texto); border: 1px solid var(--borda); }
.topo { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 20px; }
.topo .unidade { font-weight: 600; }
.topo .links { display: flex; gap: 14px; }
.topo a { color: var(--suave); font-size: 14px; text-decoration: none; }
.topo a:hover { color: var(--texto); }
.cota { background: var(--campo); border: 1px solid var(--borda); border-radius: 12px; padding: 14px 16px; margin-bottom: 20px; }
.cota-linha { display: flex; justify-content: space-between; align-items: baseline; font-size: 14px; color: var(--suave); }
.cota-linha b { font-size: 20px; color: var(--texto); }
.barra { height: 8px; border-radius: 4px; background: var(--borda); margin-top: 10px; overflow: hidden; }
.barra div { height: 100%; border-radius: 4px; background: var(--marca); }
.barra.baixa div { background: var(--aviso); }
.mensagem { text-align: center; color: var(--suave); margin: 0 0 24px; }
.botao-link {
    display: block; margin-top: 12px; padding: 12px; text-align: center; font-size: 15px; font-weight: 600;
    color: var(--primaria); text-decoration: none; border: 1px solid var(--borda); border-radius: 10px;
}
.botao-link span { display: block; font-size: 13px; font-weight: 400; color: var(--suave); }
.botao-link:hover { background: var(--primaria-clara); border-color: var(--primaria); }
.fila { margin-bottom: 20px; }
.item-fila {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px;
    border: 1px solid var(--borda); border-radius: 12px; margin-bottom: 8px; background: var(--campo);
}
.item-fila b { display: block; font-size: 15px; }
.item-fila span { font-size: 13px; color: var(--suave); }
.item-fila form { margin: 0; }
.mini-botao {
    padding: 8px 12px; font-size: 14px; font-weight: 600; border-radius: 8px; white-space: nowrap; cursor: pointer;
    color: var(--primaria); background: var(--cartao); border: 1px solid var(--borda);
}
.mini-botao.perigo { color: var(--erro); }
.mini-botao.perigo:hover { border-color: var(--erro); }
.filtro { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.filtro select {
    padding: 9px 12px; font-size: 15px; color: var(--texto); background: var(--campo);
    border: 1px solid var(--borda); border-radius: 10px;
}
.resumo { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 24px; }
.numero { background: var(--campo); border: 1px solid var(--borda); border-radius: 12px; padding: 12px 14px; }
.numero b { display: block; font-size: 24px; }
.numero span { font-size: 13px; color: var(--suave); }
.tabela { overflow-x: auto; margin-bottom: 28px; border: 1px solid var(--borda); border-radius: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--borda); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { background: var(--campo); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; color: var(--suave); }
tr:last-child td { border-bottom: none; }
td a, .voltar { color: var(--primaria); font-weight: 600; text-decoration: none; }
tr.sem-uso td { color: var(--suave); }
td.texto { text-align: left; white-space: normal; min-width: 140px; }
.voltar { display: inline-block; margin-bottom: 12px; }
.abas { display: flex; gap: 2px; margin-bottom: 20px; border-bottom: 1px solid var(--borda); overflow-x: auto; }
.abas a {
    padding: 10px 10px; color: var(--suave); font-weight: 600; text-decoration: none; white-space: nowrap; font-size: 15px;
    border-bottom: 3px solid transparent; margin-bottom: -1px;
}
.abas a.ativa { color: var(--primaria); border-bottom-color: var(--marca); }
.campos { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
.campos input, .campos select {
    width: 100%; margin: 0; padding: 11px 12px; font-size: 16px; color: var(--texto); background: var(--campo);
    border: 1px solid var(--borda); border-radius: 10px;
}
.campo-botao { display: flex; align-items: flex-end; }
.secao { border-top: 1px solid var(--borda); padding-top: 24px; margin-top: 8px; }
.dica-secao { color: var(--suave); font-size: 14px; margin: -4px 0 16px; }
.link-perigo { background: none; border: none; color: var(--erro); font-weight: 600; cursor: pointer; font-size: 14px; padding: 0; }
#copiado { text-align: center; color: var(--primaria); font-size: 14px; margin: 8px 0 0; min-height: 1.5em; }
.detalhes { border: 1px solid var(--borda); border-radius: 12px; margin-bottom: 20px; overflow: hidden; }
.detalhes div { display: flex; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--borda); font-size: 15px; }
.detalhes div:last-child { border-bottom: none; }
.detalhes span { color: var(--suave); }
.detalhes b { text-align: right; }

.livre-agora { background: var(--ok-claro); border-color: #b9dca5; }
.livre-agora .botoes { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.mini-botao.cheio { background: var(--primaria); border-color: var(--primaria); color: #fff; }
.mini-botao.cheio:hover { background: var(--primaria-hover); }

/* Agenda: abas com os dias; em cada dia, uma coluna por máquina e uma linha por horário */
.dias { display: flex; gap: 8px; overflow-x: auto; margin-bottom: 16px; padding-bottom: 4px; }
.dias a {
    flex: 0 0 auto; min-width: 74px; padding: 8px 10px; text-align: center; text-decoration: none; color: var(--texto);
    background: var(--campo); border: 1px solid var(--borda); border-radius: 12px; line-height: 1.3;
}
.dias a b { display: block; font-size: 15px; }
.dias a span { font-size: 12px; color: var(--suave); }
.dias a.ativa { background: var(--primaria); border-color: var(--primaria); color: #fff; }
.dias a.ativa span { color: var(--primaria-clara); }
.dias a.tem-escolha b::after { content: ' •'; color: var(--marca); }
section.dia { display: none; }
section.dia.ativo { display: block; }
.grade th, .grade td { text-align: center; padding: 6px; white-space: normal; }
.grade th:first-child, .grade td:first-child { text-align: left; width: 96px; white-space: nowrap; font-weight: 600; }
.grade tr.agora td:first-child { color: var(--primaria); }
.vaga { display: block; padding: 8px 4px; border-radius: 8px; font-size: 13px; font-weight: 600; border: 1px solid transparent; }
.vaga.livre { background: var(--ok-claro); color: var(--ok); border-color: #b9dca5; }
.vaga.meu { background: var(--meu-claro); color: var(--meu); }
.vaga.ocupado { background: var(--erro-claro); color: var(--erro); }
.vaga.passou { color: var(--suave); }
.escolha { display: block; cursor: pointer; position: relative; }
.escolha input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.escolha:hover .vaga.livre { border-color: var(--primaria); }
.escolha input:checked + .vaga { background: var(--primaria); color: #fff; border-color: var(--primaria); }
.escolha input:focus-visible + .vaga { outline: 2px solid var(--primaria); outline-offset: 2px; }
.escolha.bloqueada { cursor: not-allowed; }
.escolha.bloqueada .vaga { opacity: .4; }
.txt-sel { display: none; }
.escolha input:checked + .vaga .txt-livre { display: none; }
.escolha input:checked + .vaga .txt-sel { display: inline; }
.link-perigo.mini { display: block; margin: 3px auto 0; font-size: 12px; }
.legenda { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.legenda .vaga { display: inline-block; padding: 3px 10px; }
.barra-reserva {
    position: sticky; bottom: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 12px 0 4px;
    background: var(--cartao); border-top: 1px solid var(--borda);
}
.barra-reserva #aviso { flex-basis: 100%; margin: 0; padding: 8px 12px; border-radius: 8px; font-size: 14px; background: var(--ok-claro); color: var(--ok); }
.barra-reserva #aviso .mini-botao { margin-left: 8px; }
.barra-reserva span { flex: 1; font-size: 14px; color: var(--suave); }
.barra-reserva .botao { width: auto; padding: 12px 22px; }
.barra-reserva .botao:disabled { opacity: .5; cursor: not-allowed; }
</style>
</head>
<body>
<main class="cartao{% if largo %} largo{% endif %}">{{ corpo|safe }}</main>
</body>
</html>
"""

TEXTO_AGUA = ('Você reservou lavagens em horários seguidos. Lembre do uso consciente da água: junte as roupas para '
              'lavar com a máquina cheia, use o ciclo econômico quando der e deixe horários livres para as outras '
              'unidades.')
TEXTO_ENERGIA = ('Você reservou a secadora em horários seguidos. A secadora é a máquina que mais gasta energia: '
                 'centrifugue bem as roupas antes, não sobrecarregue a secadora e, se der, seque parte das roupas no '
                 'varal.')

ICONE_MAQUINA = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                 'stroke-linejoin="round"><rect x="3" y="2" width="18" height="20" rx="2"/><path d="M3 6h18"/>'
                 '<circle cx="12" cy="14" r="5"/><path d="M7 4h.01M10 4h.01"/></svg>')


def pagina(titulo, corpo, status=200, largo=False, **contexto):
    """Renderiza o conteúdo de uma tela dentro do layout comum."""
    contexto.update(titulo=titulo, icone=ICONE_MAQUINA)
    return render_template_string(HTML_BASE, titulo=titulo, largo=largo,
                                  corpo=render_template_string(corpo, **contexto)), status


HTML_LOGIN = """
<div class="icone">{{ icone|safe }}</div>
<p class="projeto">FICA</p>
<h1>Lavanderia Caburé</h1>
<p class="subtitulo">Entre com o usuário da sua unidade</p>
{% if erro %}<div class="alerta">{{ erro }}</div>{% endif %}
<form method="POST" action="/login">
    <label class="rotulo" for="usuario">Usuário</label>
    <input type="text" id="usuario" name="usuario" placeholder="ex: unidade07" autocapitalize="none"
           autocorrect="off" autocomplete="username" required autofocus>
    <label class="rotulo" for="senha">Senha</label>
    <input type="password" id="senha" name="senha" placeholder="Sua senha" autocomplete="current-password" required>
    <button type="submit" class="botao">Entrar</button>
</form>
"""

HTML_SENHA = """
<div class="topo">
    <span class="projeto">Lavanderia Caburé</span>
    <span class="links">{% if not obrigatoria %}<a href="/">Voltar</a>{% endif %}<a href="/logout">Sair</a></span>
</div>
<h1>Trocar senha</h1>
<p class="subtitulo">{% if obrigatoria %}Sua senha é provisória. Crie uma senha nova para continuar.{% else %}Escolha uma senha nova.{% endif %}</p>
{% if erro %}<div class="alerta">{{ erro }}</div>{% endif %}
<form method="POST" action="/senha">
    {% if not obrigatoria %}
    <label class="rotulo" for="atual">Senha atual</label>
    <input type="password" id="atual" name="atual" autocomplete="current-password" required>
    {% endif %}
    <label class="rotulo" for="nova">Nova senha</label>
    <input type="password" id="nova" name="nova" minlength="{{ senha_minima }}" autocomplete="new-password"
           placeholder="Pelo menos {{ senha_minima }} caracteres" required>
    <label class="rotulo" for="confirmacao">Repita a nova senha</label>
    <input type="password" id="confirmacao" name="confirmacao" minlength="{{ senha_minima }}" autocomplete="new-password" required>
    <button type="submit" class="botao">Salvar nova senha</button>
</form>
"""

HTML_INICIO = """
<div class="topo">
    <span><span class="projeto">Lavanderia</span> · <span class="unidade">{{ usuario }}</span></span>
    <span class="links"><a href="/senha">Trocar senha</a><a href="/logout">Sair</a></span>
</div>
{% if ok %}<div class="sucesso">{{ ok }}</div>{% endif %}
{% if nota_agua %}<div class="nota">💧 {{ texto_agua }}</div>{% endif %}
{% if nota_energia %}<div class="nota energia">⚡ {{ texto_energia }}</div>{% endif %}
{% if erro %}<div class="alerta">{{ erro }}</div>{% endif %}
<div class="cota">
    {% if sem_limite %}
    <div class="cota-linha"><span>Sem limite de créditos</span><span><b>{{ usados }}</b> usados nesta semana</span></div>
    {% else %}
    <div class="cota-linha"><span>Créditos restantes nesta semana</span><span><b>{{ saldo }}</b> de {{ limite }}</span></div>
    <div class="barra{% if saldo <= 1 %} baixa{% endif %}"><div style="width: {{ (saldo * 100 // limite) if limite else 0 }}%"></div></div>
    {% endif %}
</div>
{% if agora_livres %}
<div class="fila">
    <h2>Livre agora</h2>
    {% for v in agora_livres %}
    <form method="POST" action="/agenda" class="item-fila livre-agora"{% if v.so_secar %}
          onsubmit="return confirm('Vai só secar, sem lavar antes? Se for lavar também, use a lavadora primeiro (Usar agora ou Lavar e secar).')"{% endif %}>
        <input type="hidden" name="slot" value="{{ v.valor }}">
        <div><b>{{ maquinas[v.maquina] }}</b><span>pode usar até {{ v.fim[11:] }}</span></div>
        {% if sem_credito %}<span>Sem créditos</span>{% else %}
        <div class="botoes">
            <button class="mini-botao cheio">Usar agora</button>
            {% if v.secar and (sem_limite or saldo >= 2) %}<button class="mini-botao" name="slot" value="{{ v.secar }}">Lavar e secar</button>{% endif %}
        </div>
        {% endif %}
    </form>
    {% endfor %}
</div>
{% elif proxima %}
<p class="dica-secao">Nenhuma máquina livre agora. Próximo horário livre: <b>{{ maquinas[proxima.maquina] }},
    {{ dia_curto(proxima.inicio) }} às {{ proxima.inicio[11:] }}</b>.</p>
{% endif %}
<div class="fila">
    <h2>Suas reservas</h2>
    {% for a in meus %}
    {% set durante = a.inicio <= momento %}
    <div class="item-fila">
        <div><b>{{ maquinas[a.maquina] }}</b><span>{{ dia_curto(a.inicio) }}, {{ a.inicio[11:] }}–{{ a.fim[11:] }}{% if durante %} · agora{% endif %}</span></div>
        <form method="POST" action="/reserva/{{ a.id }}/cancelar"
              onsubmit="return confirm('{{ 'Liberar a máquina para outras unidades?' if durante else 'Cancelar esta reserva?' }}')">
            <button class="mini-botao perigo">{{ 'Liberar' if durante else 'Cancelar' }}</button>
        </form>
    </div>
    {% else %}
    <p class="dica-secao" style="margin: 0">Nenhuma reserva marcada.</p>
    {% endfor %}
</div>
<a href="/agenda" class="botao">Ver agenda e reservar</a>
<a href="/transparencia" class="botao-link">Transparência Caburé<span>Uso das máquinas e rateio das despesas</span></a>
<a href="/horarios" class="botao-link">Horários de funcionamento<span>Quando a lavanderia abre</span></a>
"""

HTML_AGENDA = """
<div class="topo">
    <span><span class="projeto">Agenda</span> · <span class="unidade">{{ usuario }}</span></span>
    <span class="links"><a href="/">Início</a><a href="/transparencia">Transparência</a></span>
</div>
{% if ok %}<div class="sucesso">{{ ok }}</div>{% endif %}
{% if erro %}<div class="alerta">{{ erro }}</div>{% endif %}
<div class="resumo">
    {% if sem_limite %}
    <div class="numero"><b>Sem limite</b><span>de créditos</span></div>
    {% else %}
    {% for s in saldos %}<div class="numero"><b>{{ s.restam }} de {{ limite }}</b><span>créditos restantes {{ s.nome }}</span></div>{% endfor %}
    {% endif %}
</div>
<p class="dica-secao">Toque nas máquinas livres que você vai usar e depois em <b>Reservar</b>. Cada máquina gasta 1
    crédito. A secadora é opcional: ao escolher uma lavadora, você pode incluir a secadora do horário seguinte ou
    secar no varal. Vai usar só uma lavadora? Reserve só ela: a outra fica livre para outra unidade.</p>
<nav class="dias">
    {% for d in dias %}
    <a href="/agenda?dia={{ d.chave }}" data-dia="{{ d.chave }}"{% if d.chave == selecionado %} class="ativa"{% endif %}>
        <b>{{ d.curto }}</b><span>{{ d.situacao }}</span></a>
    {% endfor %}
</nav>
<div class="legenda">
    <span class="vaga livre">Livre</span><span class="vaga meu">Sua</span><span class="vaga ocupado">Ocupada</span>
</div>
<form method="POST" action="/agenda" id="form-reserva">
    {% for d in dias %}
    <section class="dia{% if d.chave == selecionado %} ativo{% endif %}" id="dia-{{ d.chave }}">
        <h2>{{ d.titulo }}</h2>
        {% if d.linhas %}
        <div class="tabela"><table class="grade">
            <tr><th>Horário</th>{% for nome in maquinas.values() %}<th>{{ nome }}</th>{% endfor %}</tr>
            {% for l in d.linhas %}
            <tr{% if l.agora %} class="agora"{% endif %}><td>{{ l.hora }}</td>
                {% for c in l.celulas %}<td>
                    {% if c.estado == 'livre' %}
                    <label class="escolha"><input type="checkbox" name="slot" value="{{ c.valor }}" data-maquina="{{ c.maquina }}"
                        data-inicio="{{ c.inicio }}" data-fim="{{ c.fim }}" data-semana="{{ c.semana }}"><span class="vaga livre"><span class="txt-livre">Livre</span><span class="txt-sel">✓ Escolhida</span></span></label>
                    {% else %}
                    <span class="vaga {{ c.estado }}">{{ 'Sua' if c.estado == 'meu' else c.unidade }}</span>
                    {% if c.cancelar %}<button type="submit" form="c-{{ c.id }}" class="link-perigo mini">{{ 'liberar' if c.durante else 'cancelar' }}</button>{% endif %}
                    {% endif %}
                </td>{% endfor %}
            </tr>
            {% endfor %}
        </table></div>
        {% elif d.aberto %}
        <p class="dica-secao" style="margin: 0 0 24px">Não há mais horários hoje.</p>
        {% else %}
        <p class="dica-secao" style="margin: 0 0 24px">Lavanderia fechada.</p>
        {% endif %}
    </section>
    {% endfor %}
    <div class="barra-reserva">
        <p id="aviso" hidden></p>
        <span id="contagem">Nenhuma máquina escolhida</span>
        <button type="submit" class="botao" id="reservar" disabled>Reservar</button>
    </div>
</form>
{% for c in cancelaveis %}
<form id="c-{{ c.id }}" method="POST" action="/reserva/{{ c.id }}/cancelar" onsubmit="return confirm('{{ c.pergunta }}')">
    <input type="hidden" name="voltar" value="agenda">
</form>
{% endfor %}
<script>
var nomes = {{ maquinas|tojson }};
var creditos = {{ creditos|tojson }};  // null = sem limite
var caixas = Array.prototype.slice.call(document.querySelectorAll('input[name="slot"]'));
var abas = Array.prototype.slice.call(document.querySelectorAll('.dias a'));
var aviso = document.getElementById('aviso'), tempoAviso;

function hora(momento) { return momento.slice(11); }
function procurar(maquina, inicio) {
    for (var i = 0; i < caixas.length; i++) {
        if (caixas[i].dataset.maquina === maquina && caixas[i].dataset.inicio === inicio) return caixas[i];
    }
    return null;
}
var minhas = {{ minhas|tojson }};  // reservas que a unidade já tem
var avisos = {{ avisos|tojson }};

// opcoes: {botoes: [{texto, acao}], segundos, classe, tipo, alvo}
function avisar(texto, opcoes) {
    opcoes = opcoes || {};
    aviso.className = opcoes.classe || '';
    aviso.dataset.tipo = opcoes.tipo || '';
    aviso.dataset.alvo = opcoes.alvo || '';
    aviso.textContent = texto;
    (opcoes.botoes || []).forEach(function (botao) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'mini-botao'; b.textContent = botao.texto; b.onclick = botao.acao;
        aviso.appendChild(b);
    });
    aviso.hidden = false;
    clearTimeout(tempoAviso);
    tempoAviso = setTimeout(fecharAviso, (opcoes.segundos || 8) * 1000);
}
function fecharAviso() { aviso.hidden = true; aviso.dataset.tipo = ''; }

function ehSecadora(maquina) { return maquina === 'secadora'; }
function ehLavadora(maquina) { return maquina !== 'secadora'; }

// Horários (início -> fim) de um tipo de máquina: o que a unidade já reservou + o que está marcado agora
function horariosDoTipo(doTipo) {
    var fimDe = {};
    minhas.forEach(function (m) { if (doTipo(m.maquina)) fimDe[m.inicio] = m.fim; });
    caixas.forEach(function (c) { if (c.checked && doTipo(c.dataset.maquina)) fimDe[c.dataset.inicio] = c.dataset.fim; });
    return fimDe;
}
// Quantos horários seguidos do mesmo tipo de máquina, contando o desta caixa
function sequenciaCom(caixa) {
    var fimDe = horariosDoTipo(ehSecadora(caixa.dataset.maquina) ? ehSecadora : ehLavadora), inicioDe = {};
    Object.keys(fimDe).forEach(function (i) { inicioDe[fimDe[i]] = i; });
    var n = 1, h = caixa.dataset.fim;
    while (fimDe[h]) { n++; h = fimDe[h]; }
    h = caixa.dataset.inicio;
    while (inicioDe[h]) { n++; h = inicioDe[h]; }
    return n;
}
// Tem lavagem (já reservada ou marcada) terminando pouco antes desta secadora?
function lavouAntes(secadora) {
    var inicio = new Date(secadora.dataset.inicio).getTime(), janela = avisos.janela_horas * 3600 * 1000;
    var fimDe = horariosDoTipo(ehLavadora);
    return Object.keys(fimDe).some(function (i) {
        var fim = new Date(fimDe[i]).getTime();
        return fim <= inicio && inicio - fim <= janela;
    });
}

function escolhidasNaSemana(semana) {
    return caixas.filter(function (c) { return c.checked && c.dataset.semana === semana; }).length;
}
function semCredito(semana) {
    return creditos !== null && escolhidasNaSemana(semana) >= creditos[semana].restam;
}
function avisarSemCredito(semana) {
    var c = creditos[semana];
    avisar(c.restam === 0 ? 'Você não tem mais créditos ' + c.nome + '.'
        : 'Você já escolheu seus ' + c.restam + (c.restam === 1 ? ' crédito ' : ' créditos ') + c.nome
          + '. Desmarque uma máquina para trocar.');
}

// Secadora é opcional: só oferece a do horário seguinte, quem seca no varal ignora
function oferecerSecadora(lavadora) {
    var secadora = procurar('secadora', lavadora.dataset.fim);
    if (!secadora || secadora.checked || semCredito(secadora.dataset.semana)) return;
    avisar('Vai secar na secadora?', {botoes: [{
        texto: 'Incluir secadora das ' + hora(lavadora.dataset.fim),
        acao: function () {
            if (semCredito(secadora.dataset.semana)) { avisarSemCredito(secadora.dataset.semana); return; }
            secadora.checked = true; fecharAviso(); atualizar();
        }
    }]});
}

// Marcou só a secadora: pergunta se não vai lavar antes e, se der, oferece uma lavadora livre no horário anterior
function perguntarSoSecar(secadora) {
    var lavadora = null;
    caixas.forEach(function (c) {
        if (!lavadora && ehLavadora(c.dataset.maquina) && !c.checked && c.dataset.fim === secadora.dataset.inicio) lavadora = c;
    });
    var botoes = [];
    if (lavadora && !semCredito(lavadora.dataset.semana)) {
        botoes.push({texto: 'Incluir ' + nomes[lavadora.dataset.maquina] + ' das ' + hora(lavadora.dataset.inicio), acao: function () {
            if (semCredito(lavadora.dataset.semana)) { avisarSemCredito(lavadora.dataset.semana); return; }
            lavadora.checked = true; fecharAviso(); atualizar();
        }});
    }
    botoes.push({texto: 'Só secar', acao: fecharAviso});
    var texto = 'Vai só secar, sem lavar antes?';
    if (!lavadora) texto += ' (Nenhuma lavadora livre no horário anterior.)';
    else if (botoes.length === 1) texto += ' (Sem créditos para incluir uma lavadora.)';
    avisar(texto, {botoes: botoes, segundos: 15, classe: 'pergunta', tipo: 'so-secar', alvo: secadora.value});
}

// A unidade usa esta máquina neste horário (já reservada ou marcada agora)?
function usa(maquina, inicio) {
    var caixa = procurar(maquina, inicio);
    return (caixa && caixa.checked) || minhas.some(function (m) { return m.maquina === maquina && m.inicio === inicio; });
}

// Lavadora 1 às 11h e Lavadora 2 às 12h: sugere continuar na mesma lavadora, se ela estiver livre
function sugerirMesmaLavadora(c) {
    var vizinhos = [];
    minhas.forEach(function (m) { vizinhos.push(m); });
    caixas.forEach(function (o) { if (o.checked && o !== c) vizinhos.push({maquina: o.dataset.maquina, inicio: o.dataset.inicio, fim: o.dataset.fim}); });
    var anterior = null;
    vizinhos.forEach(function (v) {
        var encosta = v.fim === c.dataset.inicio || v.inicio === c.dataset.fim;
        if (!anterior && encosta && ehLavadora(v.maquina) && v.maquina !== c.dataset.maquina
            && !usa(c.dataset.maquina, v.inicio)) anterior = v;
    });
    if (!anterior) return false;
    var mesma = procurar(anterior.maquina, c.dataset.inicio);
    if (!mesma || mesma.checked) return false;
    avisar('Você já usa a ' + nomes[anterior.maquina] + ' às ' + hora(anterior.inicio) + '. Continue nela às '
        + hora(c.dataset.inicio) + ' e deixe a ' + nomes[c.dataset.maquina] + ' livre para outra unidade.', {
        segundos: 15, classe: 'pergunta', botoes: [
            {texto: 'Usar a ' + nomes[anterior.maquina], acao: function () {
                c.checked = false; mesma.checked = true; fecharAviso(); atualizar();
                if (sequenciaCom(mesma) >= avisos.lavagens) avisar('💧 ' + {{ texto_agua|tojson }}, {segundos: 15, classe: 'agua'});
            }},
            {texto: 'Manter', acao: fecharAviso}
        ]});
    return true;
}

function quandoMarcar(c) {
    if (ehLavadora(c.dataset.maquina)) {
        // Lavagem marcada depois da secadora: a pergunta "só secar" já não faz sentido
        if (aviso.dataset.tipo === 'so-secar') fecharAviso();
        if (sugerirMesmaLavadora(c)) return;
        if (sequenciaCom(c) >= avisos.lavagens) avisar('💧 ' + {{ texto_agua|tojson }}, {segundos: 15, classe: 'agua'});
        else oferecerSecadora(c);
        return;
    }
    if (sequenciaCom(c) >= avisos.secagens) avisar('⚡ ' + {{ texto_energia|tojson }}, {segundos: 15, classe: 'energia'});
    else if (!lavouAntes(c)) perguntarSoSecar(c);
}

function quandoDesmarcar(c) {
    if (aviso.dataset.tipo === 'so-secar' && aviso.dataset.alvo === c.value) fecharAviso();
}

function atualizar() {
    var escolhidas = caixas.filter(function (c) { return c.checked; });
    var n = escolhidas.length;
    document.getElementById('contagem').textContent = n === 0 ? 'Nenhuma máquina escolhida' :
        escolhidas.map(function (c) { return nomes[c.dataset.maquina] + ' ' + hora(c.dataset.inicio); }).join(', ') +
        ' · ' + n + (n === 1 ? ' crédito' : ' créditos');
    document.getElementById('reservar').disabled = n === 0;
    caixas.forEach(function (c) {
        var bloquear = !c.checked && semCredito(c.dataset.semana);
        c.disabled = bloquear;
        c.parentNode.classList.toggle('bloqueada', bloquear);
    });
    abas.forEach(function (a) {
        a.classList.toggle('tem-escolha', !!document.querySelector('#dia-' + a.dataset.dia + ' input:checked'));
    });
}

function mostrarDia(dia) {
    abas.forEach(function (a) { a.classList.toggle('ativa', a.dataset.dia === dia); });
    document.querySelectorAll('section.dia').forEach(function (s) { s.classList.toggle('ativo', s.id === 'dia-' + dia); });
    history.replaceState(null, '', '/agenda?dia=' + dia);
}

caixas.forEach(function (c) {
    c.addEventListener('change', function () {
        if (c.checked) quandoMarcar(c); else quandoDesmarcar(c);
        atualizar();
    });
    c.parentNode.addEventListener('click', function () {
        if (c.disabled) avisarSemCredito(c.dataset.semana);
    });
});
abas.forEach(function (a) {
    a.addEventListener('click', function (e) { e.preventDefault(); mostrarDia(a.dataset.dia); });
});
atualizar();
</script>
"""

HTML_HORARIOS = """
<div class="topo">
    <span class="projeto">Lavanderia Caburé</span>
    <span class="links"><a href="/">Início</a></span>
</div>
<h1>Horários de funcionamento</h1>
<p class="subtitulo">{{ maquinas.values()|join(' · ') }}</p>
<div class="detalhes">
    {% for d in dias %}
    <div><span>{{ d.nome }}</span><b>{% for p in d.periodos %}{{ p.inicio }}–{{ p.fim }}{% if not loop.last %}<br>{% endif %}{% else %}Fechado{% endfor %}</b></div>
    {% endfor %}
</div>
<a href="/agenda" class="botao">Ver agenda e reservar</a>
"""

HTML_ABAS = """
<div class="topo">
    <span class="projeto">Transparência Caburé</span>
    <span class="links"><a href="/">Lavanderia</a><a href="/logout">Sair</a></span>
</div>
<nav class="abas">
    <a href="/transparencia"{% if aba == 'uso' %} class="ativa"{% endif %}>Uso das máquinas</a>
    <a href="/transparencia/rateio"{% if aba == 'rateio' %} class="ativa"{% endif %}>Despesas e rateio</a>
</nav>
"""

HTML_USO = HTML_ABAS + """
<form method="GET" action="/transparencia" class="filtro">
    {% if unidade %}<input type="hidden" name="unidade" value="{{ unidade }}">{% endif %}
    <label class="rotulo" for="mes" style="margin: 0">Mês</label>
    <select id="mes" name="mes" onchange="this.form.submit()">
        {% for m in meses %}<option value="{{ m }}"{% if m == mes %} selected{% endif %}>{{ nome_mes(m) }}</option>{% endfor %}
    </select>
</form>

{% if unidade %}
<a class="voltar" href="/transparencia?mes={{ mes }}">← Todas as unidades</a>
<h1 style="text-align: left; margin-bottom: 16px">{{ unidade }}</h1>
<div class="resumo">
    <div class="numero"><b>{{ uso_mes.usos }}</b><span>usos em {{ nome_mes(mes) }}</span></div>
    <div class="numero"><b>{{ uso_mes.lavadora }} / {{ uso_mes.secadora }}</b><span>lavagens / secagens no mês</span></div>
    <div class="numero"><b>{{ horas(uso_mes.minutos) }}</b><span>de máquina no mês</span></div>
    <div class="numero"><b>{{ reais(gasto[unidade]) if gasto[unidade] is not none else '–' }}</b><span>gasto total (rateio)</span></div>
</div>
<h2>Usos em {{ nome_mes(mes) }}</h2>
<div class="tabela"><table>
    <tr><th>Dia</th><th>Horário</th><th>Máquina</th><th>Observação</th></tr>
    {% for a in lista_mes %}
    <tr><td>{{ dia_curto(a.inicio) }}</td><td>{{ a.inicio[11:] }}–{{ a.fim[11:] }}</td><td>{{ maquinas[a.maquina] }}</td>
        <td>{% if a.liberado_em %}liberou às {{ a.liberado_em[11:] }}{% endif %}</td></tr>
    {% else %}
    <tr class="sem-uso"><td colspan="4">Nenhum uso neste mês.</td></tr>
    {% endfor %}
</table></div>
<h2>Por mês</h2>
<div class="tabela"><table>
    <tr><th>Mês</th><th>Usos</th><th>Lavadora</th><th>Secadora</th><th>Horas</th><th>Rateio</th></tr>
    {% for m, uso, valor in por_mes %}
    <tr><td><a href="/transparencia?unidade={{ unidade }}&mes={{ m }}">{{ nome_mes(m) }}</a></td>
        <td>{{ uso.usos }}</td><td>{{ uso.lavadora }}</td><td>{{ uso.secadora }}</td><td>{{ horas(uso.minutos) }}</td>
        <td>{{ reais(valor) if valor else '–' }}</td></tr>
    {% else %}
    <tr class="sem-uso"><td colspan="6">Nenhum uso ainda.</td></tr>
    {% endfor %}
</table></div>

{% else %}
<div class="resumo">
    <div class="numero"><b>{{ uso_mes.usos }}</b><span>usos em {{ nome_mes(mes) }}</span></div>
    <div class="numero"><b>{{ uso_mes.lavadora }}</b><span>lavagens no mês</span></div>
    <div class="numero"><b>{{ uso_mes.secadora }}</b><span>secagens no mês</span></div>
    <div class="numero"><b>{{ horas(uso_mes.minutos) }}</b><span>de máquina no mês</span></div>
</div>
<h2>Por unidade</h2>
<div class="tabela"><table>
    <tr><th>Unidade</th><th>Lavadora</th><th>Secadora</th><th>Usos no mês</th><th>Horas no mês</th>
        <th>Usos (total)</th><th>Gasto total</th></tr>
    {% for u in unidades %}
    {% set p = por_unidade[u] %}
    <tr{% if not p.mes.usos %} class="sem-uso"{% endif %}>
        <td><a href="/transparencia?unidade={{ u }}&mes={{ mes }}">{{ u }}</a></td>
        <td>{{ p.mes.lavadora }}</td><td>{{ p.mes.secadora }}</td><td>{{ p.mes.usos }}</td>
        <td>{{ horas(p.mes.minutos) }}</td><td>{{ p.total.usos }}</td>
        <td>{{ reais(gasto[u]) if gasto[u] is not none else 'não paga' }}</td></tr>
    {% endfor %}
</table></div>
<h2>Por máquina em {{ nome_mes(mes) }}</h2>
<div class="tabela"><table>
    <tr><th>Máquina</th><th>Usos</th><th>Horas</th></tr>
    {% for m, nome in maquinas.items() %}
    <tr><td>{{ nome }}</td><td>{{ por_maquina[m].usos }}</td><td>{{ horas(por_maquina[m].minutos) }}</td></tr>
    {% endfor %}
</table></div>
{% endif %}
<p class="mensagem" style="font-size: 13px">Cada máquina reservada em um horário conta 1 uso. Reserva cancelada antes
    do horário não conta; quem libera a máquina no meio do horário continua com o uso contado. Gasto total = soma da
    parte de cada unidade nas despesas lançadas.</p>
"""

HTML_RATEIO = HTML_ABAS + """
<form method="GET" action="/transparencia/rateio" class="filtro">
    <label class="rotulo" for="mes" style="margin: 0">Mês</label>
    <select id="mes" name="mes" onchange="this.form.submit()">
        {% for m in meses %}<option value="{{ m }}"{% if m == mes %} selected{% endif %}>{{ nome_mes(m) }}</option>{% endfor %}
    </select>
</form>
{% if erro %}<div class="alerta">{{ erro }}</div>{% endif %}
<div class="resumo">
    <div class="numero"><b>{{ reais(total_mes) }}</b><span>despesas de {{ nome_mes(mes) }}</span></div>
    <div class="numero"><b>{{ usos_total_mes }}</b><span>usos em {{ nome_mes(mes) }}</span></div>
    <div class="numero"><b>{{ reais(total_mes / usos_total_mes) if usos_total_mes and total_mes else '–' }}</b><span>custo médio por uso</span></div>
    <div class="numero"><b>{{ reais(total_geral) }}</b><span>despesas desde o início</span></div>
</div>

<h2>Quanto cada unidade paga</h2>
<div class="tabela"><table>
    <tr><th>Unidade</th><th>Usos no mês</th><th>Valor no mês</th><th>Total desde o início</th></tr>
    {% for l in linhas %}
    <tr{% if not l.mes %} class="sem-uso"{% endif %}><td>{{ l.unidade }}</td><td>{{ l.usos }}</td>
        <td>{{ reais(l.mes) if l.mes else '–' }}</td><td>{{ reais(l.total) if l.total else '–' }}</td></tr>
    {% endfor %}
</table></div>
{% if total_mes %}
<button type="button" class="botao" id="copiar">Copiar resumo para mandar no grupo</button>
<p id="copiado"></p>
<textarea id="resumo-texto" hidden>{{ resumo }}</textarea>
{% endif %}

<div class="secao" id="despesas">
<h2>Despesas lançadas</h2>
<p class="dica-secao">Conta de luz, água, manutenção das máquinas… Cada despesa é dividida entre as {{ pagadoras|length }}
    unidades: <b>proporcional</b> ao número de usos no mês de referência (se ninguém usou, divide igual) ou em partes
    <b>iguais</b>.</p>
{% if despesas %}
<div class="tabela"><table>
    <tr><th>Mês</th><th style="text-align: left">Descrição</th><th>Valor</th><th>Divisão</th><th>Usos no mês</th><th>Custo por uso</th>{% if gestor %}<th></th>{% endif %}</tr>
    {% for d in despesas %}
    <tr><td><a href="/transparencia/rateio?mes={{ d.mes }}">{{ nome_mes(d.mes) }}</a></td><td class="texto">{{ d.descricao }}</td>
        <td>{{ reais(d.valor) }}</td><td>{{ 'proporcional' if d.dividir == 'proporcional' else 'igual' }}</td>
        <td>{{ d.usos }}</td><td>{{ reais(d.custo_uso) if d.usos else '–' }}</td>
        {% if gestor %}<td><form method="POST" action="/transparencia/despesas/{{ d.id }}/apagar" style="margin: 0"
            onsubmit="return confirm('Apagar esta despesa?')"><input type="hidden" name="mes" value="{{ mes }}">
            <button class="link-perigo">Apagar</button></form></td>{% endif %}</tr>
    {% endfor %}
</table></div>
{% else %}
<p class="dica-secao">Nenhuma despesa lançada ainda.</p>
{% endif %}
{% if gestor %}
<form method="POST" action="/transparencia/despesas" class="campos">
    <div><label class="rotulo" for="d-mes">Mês de referência</label>
        <input type="month" id="d-mes" name="mes" value="{{ mes }}" required></div>
    <div><label class="rotulo" for="d-descricao">Descrição</label>
        <input type="text" id="d-descricao" name="descricao" maxlength="80" placeholder="ex: Conta de luz" required></div>
    <div><label class="rotulo" for="d-valor">Valor (R$)</label>
        <input type="text" id="d-valor" name="valor" inputmode="decimal" placeholder="ex: 150,00" required></div>
    <div><label class="rotulo" for="d-dividir">Dividir</label>
        <select id="d-dividir" name="dividir"><option value="proporcional">Proporcional ao uso</option>
            <option value="igual">Igual entre as unidades</option></select></div>
    <div class="campo-botao"><button type="submit" class="botao">Lançar despesa</button></div>
</form>
{% endif %}
</div>
<script>
var botaoCopiar = document.getElementById('copiar');
if (botaoCopiar) botaoCopiar.addEventListener('click', function () {
    // execCommand funciona mesmo sem HTTPS (a área de transferência moderna exige HTTPS)
    var area = document.createElement('textarea');
    area.value = document.getElementById('resumo-texto').value;
    area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(area);
    document.getElementById('copiado').textContent = ok ? 'Resumo copiado! Cole na conversa do grupo.' : 'Não deu para copiar neste navegador.';
});
</script>
"""


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORTA', 5001)))
