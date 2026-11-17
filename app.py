"""
Lavanderia Caburé - Sistema de Agendamento de Lavanderia com Controle de Cotas
Reutiliza autenticação e padrões do projeto "Impressão Caburé"
"""
import csv
import json
import os
import secrets
import threading
import time
from datetime import datetime, date, timedelta
from flask import Flask, request, render_template_string, redirect, session, url_for, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
import sys
from pathlib import Path

PASTA = os.path.dirname(os.path.abspath(__file__))


def carregar_chave_secreta():
    arquivo = os.path.join(PASTA, 'chave_secreta')
    if not os.path.exists(arquivo):
        with open(arquivo, 'w') as f:
            f.write(secrets.token_hex(32))
        os.chmod(arquivo, 0o600)
    with open(arquivo) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = carregar_chave_secreta()

# Arquivos de dados
ARQUIVO_USUARIOS = os.path.join(PASTA, 'usuarios.json')
ARQUIVO_AGENDAMENTOS = os.path.join(PASTA, 'agendamentos.json')
ARQUIVO_COTAS = os.path.join(PASTA, 'cotas_lavanderia.json')
ARQUIVO_HORARIOS = os.path.join(PASTA, 'horarios_disponiveis.json')
ARQUIVO_HISTORICO = os.path.join(PASTA, 'historico_lavanderia.csv')
PASTA_BACKUPS = os.path.join(PASTA, 'backups')
DIAS_BACKUP = 30

# Configurações
SEM_LIMITE = {'fica', 'admin'}
ADMINS = {'admin'}
GESTORES = ADMINS | {'unidade32'}
SENHA_MINIMA = 6
DURACAO_SLOT_PADRAO = 60  # minutos
LIMITE_COTAS_PADRAO = 3  # usos por semana
ANTECEDENCIA_CANCELAMENTO = 24  # horas

# Travas para concorrência
trava_agendamentos = threading.Lock()
trava_cotas = threading.Lock()
trava_historico = threading.Lock()
trava_usuarios = threading.Lock()

# Limite de tentativas de login
MAX_TENTATIVAS = 5
BLOQUEIO_SEGUNDOS = 5 * 60
tentativas_falhas = {}
trava_tentativas = threading.Lock()


# ===== FUNÇÕES UTILITÁRIAS =====

def carregar_json(arquivo, padrao=None):
    if not os.path.exists(arquivo):
        return padrao or {}
    try:
        with open(arquivo) as f:
            return json.load(f)
    except:
        return padrao or {}


def salvar_json(arquivo, dados):
    os.makedirs(os.path.dirname(arquivo), exist_ok=True)
    with open(arquivo, 'w') as f:
        json.dump(dados, f, indent=2, sort_keys=True)
    if arquivo != ARQUIVO_USUARIOS:
        os.chmod(arquivo, 0o600)


def carregar_usuarios():
    try:
        with open(ARQUIVO_USUARIOS) as f:
            usuarios = json.load(f)
            for usuario, dados in usuarios.items():
                if isinstance(dados, str):
                    usuarios[usuario] = {'senha': dados, 'precisa_trocar_senha': False}
            return usuarios
    except:
        return {}


def semana_atual():
    return date.today().isocalendar()[1]


def ano_atual():
    return date.today().isocalendar()[0]


def dia_semana_nome(dia_num):
    dias = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
    return dias[dia_num % 7]


def renovar_cotas_se_necessario():
    cotas = carregar_json(ARQUIVO_COTAS)
    semana = semana_atual()
    ano = ano_atual()

    renovadas = []
    for unidade in cotas:
        chave_semana = f"{ano}-W{semana:02d}"
        if cotas[unidade].get('semana_atual') != chave_semana:
            cotas[unidade]['semana_atual'] = chave_semana
            cotas[unidade]['usos'] = 0
            renovadas.append(unidade)

    if renovadas:
        with trava_cotas:
            salvar_json(ARQUIVO_COTAS, cotas)

    return renovadas


def pode_agendar(usuario):
    if usuario in SEM_LIMITE:
        return True, "Sem limite de agendamentos"

    renovar_cotas_se_necessario()
    cotas = carregar_json(ARQUIVO_COTAS, {})

    if usuario not in cotas:
        return False, f"Usuário {usuario} não encontrado nas cotas"

    usos = cotas[usuario].get('usos', 0)
    limite = cotas[usuario].get('limite_semanal', LIMITE_COTAS_PADRAO)

    if usos >= limite:
        return False, f"Limite semanal atingido ({usos}/{limite})"

    restante = limite - usos
    return True, f"Restam {restante} agendamento(ns) esta semana"


def descontar_cota(usuario):
    if usuario in SEM_LIMITE:
        return True

    with trava_cotas:
        cotas = carregar_json(ARQUIVO_COTAS, {})
        if usuario in cotas:
            cotas[usuario]['usos'] = cotas[usuario].get('usos', 0) + 1
            salvar_json(ARQUIVO_COTAS, cotas)
            return True
    return False


def devolver_cota(usuario):
    if usuario in SEM_LIMITE:
        return True

    with trava_cotas:
        cotas = carregar_json(ARQUIVO_COTAS, {})
        if usuario in cotas and cotas[usuario].get('usos', 0) > 0:
            cotas[usuario]['usos'] -= 1
            salvar_json(ARQUIVO_COTAS, cotas)
            return True
    return False


def registrar_no_historico(unidade, acao, detalhes):
    with trava_historico:
        existe = os.path.exists(ARQUIVO_HISTORICO)
        with open(ARQUIVO_HISTORICO, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['data_hora', 'unidade', 'acao', 'detalhes'])
            if not existe:
                writer.writeheader()
            writer.writerow({
                'data_hora': datetime.now().isoformat(),
                'unidade': unidade,
                'acao': acao,
                'detalhes': detalhes
            })


def fazer_backup():
    if not os.path.exists(PASTA_BACKUPS):
        os.makedirs(PASTA_BACKUPS)

    hoje = date.today().isoformat()
    pasta_backup = os.path.join(PASTA_BACKUPS, hoje)

    if not os.path.exists(pasta_backup):
        os.makedirs(pasta_backup)
        for arquivo in [ARQUIVO_AGENDAMENTOS, ARQUIVO_COTAS, ARQUIVO_HISTORICO]:
            if os.path.exists(arquivo):
                import shutil
                shutil.copy(arquivo, pasta_backup)


# Fazer backup na primeira requisição do dia
ultima_data_backup = [None]


def garantir_backup_diario():
    global ultima_data_backup
    hoje = date.today().isoformat()
    if ultima_data_backup[0] != hoje:
        fazer_backup()
        ultima_data_backup[0] = hoje


# ===== ROTAS DE AUTENTICAÇÃO =====

@app.before_request
def validar_bloqueio_ip():
    ip = request.remote_addr
    with trava_tentativas:
        if ip in tentativas_falhas:
            erros, ultima_tentativa = tentativas_falhas[ip]
            if time.time() - ultima_tentativa < BLOQUEIO_SEGUNDOS and erros >= MAX_TENTATIVAS:
                return "IP bloqueado por muitas tentativas. Tente novamente em alguns minutos.", 429
            if time.time() - ultima_tentativa > BLOQUEIO_SEGUNDOS:
                del tentativas_falhas[ip]


@app.before_request
def carregar_sessao_usuario():
    request.usuario = session.get('usuario')
    request.eh_admin = request.usuario in ADMINS
    request.eh_gestor = request.usuario in GESTORES
    garantir_backup_diario()


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip().lower()
        senha = request.form.get('senha', '')
        ip = request.remote_addr

        usuarios = carregar_usuarios()

        if usuario in usuarios and check_password_hash(usuarios[usuario]['senha'], senha):
            with trava_tentativas:
                if ip in tentativas_falhas:
                    del tentativas_falhas[ip]

            session['usuario'] = usuario
            if usuarios[usuario].get('precisa_trocar_senha'):
                return redirect(url_for('mudar_senha'))
            return redirect(url_for('dashboard'))

        with trava_tentativas:
            if ip not in tentativas_falhas:
                tentativas_falhas[ip] = [1, time.time()]
            else:
                erros, _ = tentativas_falhas[ip]
                tentativas_falhas[ip] = [erros + 1, time.time()]

        return render_template_string(HTML_LOGIN, erro="Usuário ou senha inválidos"), 401

    return render_template_string(HTML_LOGIN)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/senha', methods=['GET', 'POST'])
def mudar_senha():
    if not request.usuario:
        return redirect(url_for('login'))

    if request.method == 'POST':
        senha_atual = request.form.get('senha_atual', '')
        senha_nova = request.form.get('senha_nova', '')

        usuarios = carregar_usuarios()
        if request.usuario not in usuarios:
            return "Usuário não encontrado", 404

        if not check_password_hash(usuarios[request.usuario]['senha'], senha_atual):
            return render_template_string(HTML_MUDAR_SENHA, erro="Senha atual incorreta"), 401

        if len(senha_nova) < SENHA_MINIMA:
            return render_template_string(HTML_MUDAR_SENHA,
                                        erro=f"Senha deve ter pelo menos {SENHA_MINIMA} caracteres"), 400

        with trava_usuarios:
            usuarios = carregar_usuarios()
            usuarios[request.usuario]['senha'] = generate_password_hash(senha_nova)
            usuarios[request.usuario]['precisa_trocar_senha'] = False
            salvar_json(ARQUIVO_USUARIOS, usuarios)

        registrar_no_historico(request.usuario, 'troca_senha', 'Senha alterada com sucesso')
        return redirect(url_for('dashboard'))

    return render_template_string(HTML_MUDAR_SENHA)


# ===== ROTAS PRINCIPAIS =====

@app.route('/')
def index():
    if not request.usuario:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    if not request.usuario:
        return redirect(url_for('login'))

    agendamentos = carregar_json(ARQUIVO_AGENDAMENTOS, {})
    meus_agendamentos = []

    for id_agenda, dados in agendamentos.items():
        if dados.get('unidade') == request.usuario and not dados.get('cancelado'):
            meus_agendamentos.append(dados)

    meus_agendamentos.sort(key=lambda x: x.get('data_hora', ''))

    pode_agendar_flag, msg_cota = pode_agendar(request.usuario)

    return render_template_string(HTML_DASHBOARD,
                                usuario=request.usuario,
                                agendamentos=meus_agendamentos,
                                pode_agendar=pode_agendar_flag,
                                msg_cota=msg_cota)


@app.route('/agendar', methods=['GET', 'POST'])
def agendar():
    if not request.usuario:
        return redirect(url_for('login'))

    pode_agendar_flag, msg_cota = pode_agendar(request.usuario)
    if not pode_agendar_flag:
        return render_template_string(HTML_AGENDAR, erro=msg_cota), 403

    if request.method == 'POST':
        data_str = request.form.get('data', '')
        hora_str = request.form.get('hora', '')

        try:
            data_hora_str = f"{data_str}T{hora_str}:00"
            data_hora = datetime.fromisoformat(data_hora_str)

            if data_hora < datetime.now():
                return render_template_string(HTML_AGENDAR,
                                            erro="Não é possível agendar no passado"), 400

            with trava_agendamentos:
                agendamentos = carregar_json(ARQUIVO_AGENDAMENTOS, {})

                # Verificar se slot está livre
                for id_agenda, dados in agendamentos.items():
                    if (dados.get('data_hora') == data_hora_str and
                        not dados.get('cancelado')):
                        return render_template_string(HTML_AGENDAR,
                                                    erro="Este horário já está reservado"), 409

                # Criar novo agendamento
                id_novo = str(int(datetime.now().timestamp()))
                agendamentos[id_novo] = {
                    'id': id_novo,
                    'unidade': request.usuario,
                    'data_hora': data_hora_str,
                    'duracao_minutos': DURACAO_SLOT_PADRAO,
                    'confirmado': True,
                    'data_reserva': datetime.now().isoformat(),
                    'cancelado': False,
                    'data_cancelamento': None,
                    'motivo_cancelamento': None
                }

                # Descontar cota
                if descontar_cota(request.usuario):
                    salvar_json(ARQUIVO_AGENDAMENTOS, agendamentos)
                    registrar_no_historico(request.usuario, 'agendamento',
                                          f"Agendamento criado para {data_hora_str}")
                    return redirect(url_for('confirmacao_agendamento', id_agenda=id_novo))
                else:
                    return render_template_string(HTML_AGENDAR,
                                                erro="Erro ao descontar cota"), 500
        except ValueError:
            return render_template_string(HTML_AGENDAR,
                                        erro="Data ou hora inválida"), 400

    horarios = carregar_json(ARQUIVO_HORARIOS, {})
    return render_template_string(HTML_AGENDAR, horarios=horarios, msg_cota=msg_cota)


@app.route('/confirmacao/<id_agenda>')
def confirmacao_agendamento(id_agenda):
    if not request.usuario:
        return redirect(url_for('login'))

    agendamentos = carregar_json(ARQUIVO_AGENDAMENTOS, {})
    if id_agenda not in agendamentos:
        return "Agendamento não encontrado", 404

    agenda = agendamentos[id_agenda]
    if agenda.get('unidade') != request.usuario and not request.eh_gestor:
        return "Acesso negado", 403

    return render_template_string(HTML_CONFIRMACAO, agendamento=agenda)


@app.route('/cancelar/<id_agenda>', methods=['POST'])
def cancelar_agendamento(id_agenda):
    if not request.usuario:
        return redirect(url_for('login'))

    with trava_agendamentos:
        agendamentos = carregar_json(ARQUIVO_AGENDAMENTOS, {})
        if id_agenda not in agendamentos:
            return "Agendamento não encontrado", 404

        agenda = agendamentos[id_agenda]
        if agenda.get('unidade') != request.usuario and not request.eh_gestor:
            return "Acesso negado", 403

        # Validar antecedência
        data_hora = datetime.fromisoformat(agenda['data_hora'])
        horas_restantes = (data_hora - datetime.now()).total_seconds() / 3600

        if horas_restantes < ANTECEDENCIA_CANCELAMENTO and not request.eh_gestor:
            return f"Cancelamento deve ser feito com pelo menos {ANTECEDENCIA_CANCELAMENTO}h de antecedência", 403

        # Cancelar
        agenda['cancelado'] = True
        agenda['data_cancelamento'] = datetime.now().isoformat()
        agenda['motivo_cancelamento'] = request.form.get('motivo', 'Cancelado pelo morador')

        agendamentos[id_agenda] = agenda
        salvar_json(ARQUIVO_AGENDAMENTOS, agendamentos)

        # Devolver cota
        devolver_cota(agenda['unidade'])

        registrar_no_historico(request.usuario, 'cancelamento',
                              f"Cancelado agendamento {id_agenda}")

    return redirect(url_for('dashboard'))


@app.route('/saldo')
def saldo_cotas():
    if not request.usuario:
        return jsonify({'erro': 'Não autenticado'}), 401

    pode_agendar_flag, msg = pode_agendar(request.usuario)

    cotas = carregar_json(ARQUIVO_COTAS, {})
    usuario_cotas = cotas.get(request.usuario, {})

    return jsonify({
        'usuario': request.usuario,
        'pode_agendar': pode_agendar_flag,
        'mensagem': msg,
        'usos': usuario_cotas.get('usos', 0),
        'limite': usuario_cotas.get('limite_semanal', LIMITE_COTAS_PADRAO),
        'restante': max(0, usuario_cotas.get('limite_semanal', LIMITE_COTAS_PADRAO) - usuario_cotas.get('usos', 0)),
        'semana_atual': usuario_cotas.get('semana_atual', 'indefinida')
    })


# ===== ROTAS DE GESTORES =====

@app.route('/gestao')
def gestao():
    if not request.eh_gestor:
        return "Acesso negado", 403

    agendamentos = carregar_json(ARQUIVO_AGENDAMENTOS, {})
    cotas = carregar_json(ARQUIVO_COTAS, {})

    agendamentos_ativos = [a for a in agendamentos.values() if not a.get('cancelado')]

    # Estatísticas
    total_agendamentos = len(agendamentos_ativos)
    usuarios_unicos = set(a.get('unidade') for a in agendamentos_ativos)
    top_usuarios = sorted([(u, len([a for a in agendamentos_ativos if a.get('unidade') == u]))
                           for u in usuarios_unicos], key=lambda x: x[1], reverse=True)[:5]

    return render_template_string(HTML_GESTAO,
                                agendamentos=agendamentos_ativos,
                                cotas=cotas,
                                total_agendamentos=total_agendamentos,
                                top_usuarios=top_usuarios)


@app.route('/gestao/cotas/<unidade>', methods=['POST'])
def resetar_cota(unidade):
    if not request.eh_gestor:
        return "Acesso negado", 403

    with trava_cotas:
        cotas = carregar_json(ARQUIVO_COTAS, {})
        if unidade in cotas:
            semana = semana_atual()
            ano = ano_atual()
            cotas[unidade]['semana_atual'] = f"{ano}-W{semana:02d}"
            cotas[unidade]['usos'] = 0
            salvar_json(ARQUIVO_COTAS, cotas)
            registrar_no_historico(request.usuario, 'reset_cota', f"Cota resetada para {unidade}")

    return redirect(url_for('gestao'))


# ===== TEMPLATES HTML =====

HTML_LOGIN = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lavanderia Caburé - Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
               background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
               min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .login-box { background: white; padding: 2rem; border-radius: 10px; box-shadow: 0 10px 25px rgba(0,0,0,0.2);
                     width: 100%; max-width: 400px; }
        h1 { color: #333; margin-bottom: 1.5rem; text-align: center; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.5rem; color: #555; font-weight: 500; }
        input { width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 5px;
                font-size: 1rem; }
        input:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
        button { width: 100%; padding: 0.75rem; background: #667eea; color: white; border: none;
                 border-radius: 5px; font-size: 1rem; cursor: pointer; font-weight: 600; }
        button:hover { background: #5568d3; }
        .erro { background: #fee; color: #c33; padding: 1rem; border-radius: 5px; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🧺 Lavanderia Caburé</h1>
        {% if erro %}
            <div class="erro">{{ erro }}</div>
        {% endif %}
        <form method="post">
            <div class="form-group">
                <label for="usuario">Unidade</label>
                <input type="text" id="usuario" name="usuario" required autofocus>
            </div>
            <div class="form-group">
                <label for="senha">Senha</label>
                <input type="password" id="senha" name="senha" required>
            </div>
            <button type="submit">Entrar</button>
        </form>
    </div>
</body>
</html>'''

HTML_DASHBOARD = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lavanderia Caburé - Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
               background: #f5f5f5; }
        .container { max-width: 900px; margin: 0 auto; padding: 1rem; }
        header { background: #667eea; color: white; padding: 1.5rem; border-radius: 10px;
                 margin-bottom: 2rem; display: flex; justify-content: space-between; align-items: center; }
        h1 { font-size: 1.5rem; }
        .user-info { text-align: right; }
        .user-info p { margin-bottom: 0.5rem; }
        a { color: white; text-decoration: none; cursor: pointer; }
        a:hover { text-decoration: underline; }
        .cartao { background: white; padding: 1.5rem; border-radius: 10px; margin-bottom: 1.5rem;
                  box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .botao { display: inline-block; padding: 0.75rem 1.5rem; background: #667eea;
                 color: white; text-decoration: none; border-radius: 5px; border: none;
                 cursor: pointer; font-weight: 600; }
        .botao:hover { background: #5568d3; }
        .botao.secondary { background: #999; }
        .botao.secondary:hover { background: #777; }
        .cota-info { background: #f0f0f0; padding: 1rem; border-radius: 5px; margin-bottom: 1rem; }
        .agenda-list { list-style: none; }
        .agenda-item { background: #f9f9f9; padding: 1rem; border-left: 4px solid #667eea;
                      margin-bottom: 0.5rem; border-radius: 5px; display: flex;
                      justify-content: space-between; align-items: center; }
        .agenda-item p { margin: 0.25rem 0; }
        .agenda-item .hora { font-weight: bold; }
        .vazio { text-align: center; color: #999; padding: 2rem; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🧺 Lavanderia Caburé</h1>
            <div class="user-info">
                <p>Olá, <strong>{{ usuario }}</strong></p>
                <a href="{{ url_for('mudar_senha') }}">Mudar senha</a> | <a href="{{ url_for('logout') }}">Sair</a>
            </div>
        </header>

        <div class="cartao">
            <h2>Saldo de Agendamentos</h2>
            <div class="cota-info">
                <p><strong>{{ msg_cota }}</strong></p>
                {% if pode_agendar %}
                    <button class="botao" onclick="location.href='{{ url_for('agendar') }}'">+ Novo Agendamento</button>
                {% else %}
                    <p style="color: #c33; margin-top: 1rem;">⚠️ Você atingiu o limite de agendamentos esta semana.</p>
                {% endif %}
            </div>
        </div>

        <div class="cartao">
            <h2>Meus Agendamentos</h2>
            {% if agendamentos %}
                <ul class="agenda-list">
                    {% for agenda in agendamentos %}
                    <li class="agenda-item">
                        <div>
                            <p class="hora">📅 {{ agenda.data_hora }}</p>
                            <p>Duração: {{ agenda.duracao_minutos }} minutos</p>
                        </div>
                        <div>
                            <form method="post" action="{{ url_for('cancelar_agendamento', id_agenda=agenda.id) }}" style="display: inline;">
                                <input type="hidden" name="motivo" value="Cancelado pelo morador">
                                <button type="submit" class="botao secondary">Cancelar</button>
                            </form>
                        </div>
                    </li>
                    {% endfor %}
                </ul>
            {% else %}
                <p class="vazio">Você ainda não tem agendamentos.</p>
            {% endif %}
        </div>
    </div>
</body>
</html>'''

HTML_AGENDAR = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lavanderia Caburé - Agendar</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
               background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; padding: 1rem; }
        header { background: #667eea; color: white; padding: 1rem; border-radius: 10px;
                 margin-bottom: 2rem; }
        .cartao { background: white; padding: 2rem; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h2 { margin-bottom: 1.5rem; color: #333; }
        .form-group { margin-bottom: 1.5rem; }
        label { display: block; margin-bottom: 0.5rem; color: #555; font-weight: 500; }
        input, select { width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 5px;
                        font-size: 1rem; }
        input:focus, select:focus { outline: none; border-color: #667eea; }
        .botao { padding: 0.75rem 1.5rem; background: #667eea; color: white; border: none;
                 border-radius: 5px; cursor: pointer; font-weight: 600; font-size: 1rem; }
        .botao:hover { background: #5568d3; }
        .erro { background: #fee; color: #c33; padding: 1rem; border-radius: 5px; margin-bottom: 1rem; }
        .info { background: #f0f0f0; padding: 1rem; border-radius: 5px; margin-bottom: 1rem; }
        a { color: #667eea; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🧺 Novo Agendamento</h1>
        </header>

        <div class="cartao">
            {% if erro %}
                <div class="erro">{{ erro }}</div>
            {% endif %}

            {% if pode_agendar %}
                <div class="info">{{ msg_cota }}</div>

                <form method="post">
                    <div class="form-group">
                        <label for="data">Data</label>
                        <input type="date" id="data" name="data" required min="{{ today }}">
                    </div>

                    <div class="form-group">
                        <label for="hora">Horário</label>
                        <select id="hora" name="hora" required>
                            <option value="">Selecione um horário</option>
                            <option value="08:00">08:00 - 09:00</option>
                            <option value="09:00">09:00 - 10:00</option>
                            <option value="10:00">10:00 - 11:00</option>
                            <option value="11:00">11:00 - 12:00</option>
                            <option value="14:00">14:00 - 15:00</option>
                            <option value="15:00">15:00 - 16:00</option>
                            <option value="16:00">16:00 - 17:00</option>
                            <option value="17:00">17:00 - 18:00</option>
                        </select>
                    </div>

                    <button type="submit" class="botao">Agendar</button>
                </form>
            {% endif %}

            <p style="margin-top: 1.5rem;"><a href="{{ url_for('dashboard') }}">← Voltar ao dashboard</a></p>
        </div>
    </div>

    <script>
        // Definir data mínima como hoje
        const dataInput = document.getElementById('data');
        const hoje = new Date().toISOString().split('T')[0];
        dataInput.min = hoje;
    </script>
</body>
</html>'''

HTML_CONFIRMACAO = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lavanderia Caburé - Confirmação</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
               background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; padding: 1rem; }
        .sucesso { background: white; padding: 2rem; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                   text-align: center; }
        .icone { font-size: 3rem; margin-bottom: 1rem; }
        h1 { color: #333; margin-bottom: 1rem; }
        .detalhes { background: #f0f0f0; padding: 1.5rem; border-radius: 5px; margin: 1.5rem 0;
                    text-align: left; }
        .detalhes p { margin: 0.5rem 0; }
        a { display: inline-block; margin-top: 1rem; padding: 0.75rem 1.5rem; background: #667eea;
            color: white; text-decoration: none; border-radius: 5px; cursor: pointer; }
        a:hover { background: #5568d3; }
    </style>
</head>
<body>
    <div class="container">
        <div class="sucesso">
            <div class="icone">✅</div>
            <h1>Agendamento Confirmado!</h1>

            <div class="detalhes">
                <p><strong>Data e Hora:</strong> {{ agendamento.data_hora }}</p>
                <p><strong>Duração:</strong> {{ agendamento.duracao_minutos }} minutos</p>
                <p><strong>ID do Agendamento:</strong> {{ agendamento.id }}</p>
            </div>

            <p>⏰ Você pode cancelar este agendamento até 24 horas antes do horário marcado.</p>

            <a href="{{ url_for('dashboard') }}">Voltar ao Dashboard</a>
        </div>
    </div>
</body>
</html>'''

HTML_MUDAR_SENHA = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lavanderia Caburé - Mudar Senha</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
               background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; padding: 1rem; }
        .cartao { background: white; padding: 2rem; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { margin-bottom: 1.5rem; color: #333; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.5rem; color: #555; font-weight: 500; }
        input { width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 5px; }
        input:focus { outline: none; border-color: #667eea; }
        button { padding: 0.75rem 1.5rem; background: #667eea; color: white; border: none;
                 border-radius: 5px; cursor: pointer; font-weight: 600; }
        button:hover { background: #5568d3; }
        .erro { background: #fee; color: #c33; padding: 1rem; border-radius: 5px; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <div class="container">
        <div class="cartao">
            <h1>Mudar Senha</h1>

            {% if erro %}
                <div class="erro">{{ erro }}</div>
            {% endif %}

            <form method="post">
                <div class="form-group">
                    <label for="senha_atual">Senha Atual</label>
                    <input type="password" id="senha_atual" name="senha_atual" required>
                </div>

                <div class="form-group">
                    <label for="senha_nova">Nova Senha</label>
                    <input type="password" id="senha_nova" name="senha_nova" required
                           minlength="{{ min_senha }}">
                    <small>Mínimo 6 caracteres</small>
                </div>

                <button type="submit">Alterar Senha</button>
            </form>
        </div>
    </div>
</body>
</html>'''

HTML_GESTAO = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lavanderia Caburé - Gestão</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
               background: #f5f5f5; }
        .container { max-width: 1000px; margin: 0 auto; padding: 1rem; }
        header { background: #667eea; color: white; padding: 1rem; border-radius: 10px; margin-bottom: 2rem; }
        .cartao { background: white; padding: 1.5rem; border-radius: 10px; margin-bottom: 1.5rem;
                  box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h2 { margin-bottom: 1rem; color: #333; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #f0f0f0; font-weight: 600; }
        a { color: #667eea; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🧺 Gestão - Lavanderia Caburé</h1>
            <p><a href="{{ url_for('dashboard') }}" style="color: white;">← Dashboard</a></p>
        </header>

        <div class="cartao">
            <h2>Estatísticas</h2>
            <p>Total de agendamentos: <strong>{{ total_agendamentos }}</strong></p>
            <p>Usuários únicos: <strong>{{ top_usuarios|length }}</strong></p>
        </div>

        <div class="cartao">
            <h2>Top Usuários</h2>
            <table>
                <thead>
                    <tr><th>Unidade</th><th>Agendamentos</th></tr>
                </thead>
                <tbody>
                    {% for usuario, count in top_usuarios %}
                    <tr>
                        <td>{{ usuario }}</td>
                        <td>{{ count }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="cartao">
            <h2>Próximos Agendamentos</h2>
            <table>
                <thead>
                    <tr><th>Data/Hora</th><th>Unidade</th><th>Duração</th></tr>
                </thead>
                <tbody>
                    {% for agenda in agendamentos[:10] %}
                    <tr>
                        <td>{{ agenda.data_hora }}</td>
                        <td>{{ agenda.unidade }}</td>
                        <td>{{ agenda.duracao_minutos }}min</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>'''


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
