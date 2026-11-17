# 🧺 Lavanderia Caburé

Sistema seguro de agendamento de lavanderia com controle de cotas, desenvolvido como evolução do projeto "Impressão Caburé".

## Visão Geral

**Problema**: Moradores estavam usando a lavanderia de forma desorganizada, burlando o sistema via planilha Google Sheets.

**Solução**: Sistema web baseado em Flask com:
- ✅ Autenticação segura por unidade (mesmas credenciais do sistema de impressão)
- ✅ Sistema de cotas (máximo de usos por semana configurável)
- ✅ Agendamento de horários com bloqueio automático
- ✅ Painel de gestão para administradores
- ✅ Histórico de uso em CSV
- ✅ Backup automático diário

## Requisitos

- Python 3.8+
- Flask 2.3+
- Werkzeug 2.3+

## Instalação

```bash
# 1. Clonar/baixar o projeto
cd /home/fel/lavanderia-cabure

# 2. Instalar dependências
pip3 install -r requirements.txt

# 3. (Opcional) Criar ambiente virtual
python3 -m venv venv
source venv/bin/activate
```

## Configuração

### 1. Usuários e Senhas

O sistema herda **automaticamente** os usuários e senhas do projeto "Impressão Caburé":

```bash
# Para gerar/resetar senhas, use o script da Impressão Caburé:
cd /home/fel/servidor_impressao
python3 gerar_senhas.py        # Gera senhas para todas as unidades
python3 gerar_senhas.py admin  # Cria usuário admin
```

As senhas geradas valem para **ambos os sistemas** (Impressão e Lavanderia).

### 2. Cotas de Agendamento

Padrão: **3 usos por semana** por morador

```bash
# Listar todas as cotas
python3 cotas_lavanderia.py

# Resetar todas as cotas (segunda da semana)
python3 cotas_lavanderia.py reset

# Resetar cota de uma unidade específica
python3 cotas_lavanderia.py reset unidade01

# Ajustar limite de uma unidade
python3 cotas_lavanderia.py set unidade01 5  # 5 usos por semana
```

### 3. Horários de Operação

Edite `horarios_disponiveis.json` para configurar horários, duração dos slots e capacidade:

```json
{
  "segunda": [
    {
      "inicio": "08:00",
      "fim": "12:00",
      "duracao_slots_minutos": 60,
      "slots_simultaneos": 2
    }
  ]
}
```

**Padrão atual**:
- Segunda a sexta: 08:00-12:00 e 14:00-18:00 (2 máquinas simultâneas)
- Sábado: 08:00-12:00 (1 máquina)
- Domingo: Fechado

## Uso

### Iniciar o servidor

```bash
python3 app.py
```

O servidor roda em `http://localhost:5000`

### Interface do Morador

1. **Login**: Usa sua unidade (ex: `unidade01`) e senha
2. **Dashboard**: Vê seus agendamentos e saldo de cotas
3. **Novo Agendamento**: 
   - Seleciona data e horário
   - Sistema valida automaticamente se há saldo e se slot está livre
   - Confirmação instantânea
4. **Cancelar**: Pode cancelar até 24h antes do agendamento

### Painel de Gestão

Acesse em `/gestao` com usuário `admin` ou `unidade32`:

- **Estatísticas**: Total de agendamentos, usuários mais ativos
- **Calendário**: Visão de ocupação por dia
- **Cotas**: Resetar cotas de unidades específicas
- **Relatórios**: Exportar histórico em CSV

## Arquivos Importantes

| Arquivo | Descrição |
|---------|-----------|
| `app.py` | Aplicação Flask principal |
| `usuarios.json` | Usuários e senhas (compartilhado com Impressão Caburé) |
| `agendamentos.json` | Registro de todos os agendamentos |
| `cotas_lavanderia.json` | Cotas atuais por unidade |
| `horarios_disponiveis.json` | Configuração de horários de operação |
| `historico_lavanderia.csv` | Log de todas as ações (auditoria) |
| `cotas_lavanderia.py` | Script de gestão de cotas |

## Segurança

- ✅ Senhas com hash seguro (Werkzeug)
- ✅ Chave de sessão aleatória (`chave_secreta`)
- ✅ Bloqueio por tentativas de login falhas (5 erros = 5 min bloqueado)
- ✅ Autenticação necessária em todas as rotas
- ✅ Validação de autorização (morador só vê seus próprios agendamentos)
- ✅ Histórico de auditoria (CSV com data/hora/ação)
- ✅ Backup automático diário

## Fluxo de Cotas

1. **Renovação**: Cotas reset **toda segunda-feira** automaticamente
2. **Validação**: Ao tentar agendar, sistema verifica saldo
3. **Desconto**: Ao confirmar agendamento, cota é descontada imediatamente
4. **Devolução**: Se cancelar agendamento, cota é devolvida

**Sem limite**: Usuários em `SEM_LIMITE = {'fica', 'admin'}` podem agendar indefinidamente

## Logs e Histórico

Todas as ações são registradas em `historico_lavanderia.csv`:

```csv
data_hora,unidade,acao,detalhes
2026-11-17T10:30:45.123456,unidade01,agendamento,Agendamento criado para 2026-11-20T08:00:00
2026-11-17T10:31:20.654321,unidade01,cancelamento,Cancelado agendamento 1731842400
```

Abra em Excel/Calc para análise.

## Troubleshooting

### "Usuário ou senha inválidos"
- Confirme que o usuário existe em `/home/fel/servidor_impressao/usuarios.json`
- Resetar senha: `python3 /home/fel/servidor_impressao/gerar_senhas.py unidade01`

### "Limite de agendamentos atingido"
- Verificar saldo: `python3 cotas_lavanderia.py`
- Resetar cota: `python3 cotas_lavanderia.py reset unidade01`

### "IP bloqueado"
- Aguarde 5 minutos após limite de tentativas falhas
- Ou reinicie o servidor: `python3 app.py`

### Slots não aparecem no calendário
- Verificar `horarios_disponiveis.json` está correto
- Validar JSON: `python3 -m json.tool horarios_disponiveis.json`

## Integração com Impressão Caburé

Ambos os sistemas compartilham:
- **Usuários e senhas** (`usuarios.json`)
- **Padrão de autenticação** (Flask + Werkzeug)
- **Design visual** (templates HTML similar)
- **Padrão de gestão** (admin, unidade32 como gestores)

### Dados Separados

Cada sistema mantém seus próprios dados:
- Impressão Caburé: `trabalhos.json`, `cotas.json`, `historico.csv`
- Lavanderia Caburé: `agendamentos.json`, `cotas_lavanderia.json`, `historico_lavanderia.csv`

## Próximos Passos (Pós-MVP)

- [ ] Email de lembretes 24h antes do agendamento
- [ ] QR code para check-in na lavanderia
- [ ] Calendário interativo com drag-drop
- [ ] Integração com sistema de portaria
- [ ] Relatórios avançados (padrões de uso, horas pico)
- [ ] Suporte a múltiplas máquinas
- [ ] Bloqueio por manutenção/indisponibilidade

## Suporte

Para problemas ou sugestões:
1. Verificar `historico_lavanderia.csv` para entender sequência de eventos
2. Consultar `cotas_lavanderia.py` para ajustes de cotas
3. Revisar `app.py` linha X (erros no servidor aparecem no terminal)

---

**Desenvolvido com** ❤️ **para a comunidade Caburé**

Versão 1.0 - Sistema de Agendamento de Lavanderia
