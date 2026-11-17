# Lavanderia Caburé

Reserva das máquinas da lavanderia (2 lavadoras e 1 secadora), com limite de usos por semana e um portal de
transparência do uso e do rateio das despesas. Mesmo visual e mesmo esquema de login da Impressão Caburé.

## Rodar

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python app.py    # http://localhost:5001  (outra porta: PORTA=5002 venv/bin/python app.py)
```

Como serviço (liga com o computador e volta sozinho se cair), veja as instruções no topo de
`lavanderia-cabure.service`.

**Usuários e senhas são os mesmos da Impressão Caburé**: a lavanderia lê `../servidor_impressao/usuarios.json`
(outra pasta: variável `PASTA_IMPRESSAO`). Trocar a senha em um sistema vale para o outro, e a troca também
atualiza o `senhas_unidades.txt` da impressão. Para criar ou resetar senhas, use os scripts da impressão.

## Como funciona

- **Reserva por máquina**: na agenda (próximos 7 dias), cada horário tem uma coluna para Lavadora 1, Lavadora 2 e
  Secadora. Quem vai usar só uma lavadora reserva só ela, e as outras continuam livres para outras unidades.
- **Créditos**: 6 por semana (`LIMITE_SEMANAL` em `app.py`). Cada máquina reservada em um horário gasta 1 crédito
  (lavar e secar = 2). A agenda mostra quem reservou cada máquina.
- **Uso consciente da água e da energia**: quem reserva 3 lavagens (`LAVAGENS_SEGUIDAS_AVISO`) ou 3 secagens
  (`SECAGENS_SEGUIDAS_AVISO`) em horários seguidos vê uma nota na agenda e depois de reservar. As sequências somam
  as reservas que a unidade já tinha. A reserva não é bloqueada.
- **Só secar?**: ao marcar a secadora sem uma lavagem terminando até 2 horas antes (`JANELA_LAVAR_SECAR_HORAS`), a
  agenda pergunta se a pessoa vai só secar e oferece uma lavadora livre no horário anterior.
- **Cancelar / liberar**: antes do horário, cancelar devolve o uso. Durante o horário, "Liberar" deixa a máquina
  livre pelo resto do horário (o uso continua contado). Gestores cancelam reservas de qualquer unidade.
- **Sem limite**: `fica` e `admin` (também não entram no rateio). **Gestores**: `admin` e `unidade32`.

## Transparência Caburé (`/transparencia`)

Todas as unidades logadas veem:

- **Uso das máquinas**: usos por unidade e por máquina no mês, horas de máquina, total desde o início e o gasto
  total de cada unidade. Clicando numa unidade aparece o detalhe de cada uso.
- **Despesas e rateio**: gestores lançam despesas (luz, água, manutenção…) com o mês de referência. Cada despesa é
  dividida proporcional ao número de usos no mês (ou igual entre as unidades). Tem botão para copiar o resumo e
  mandar no grupo.

## Ajustes

```bash
python3 cotas_lavanderia.py                          # usos desta semana de cada unidade
python3 cotas_lavanderia.py limite unidade05 5       # limite próprio para uma unidade
python3 cotas_lavanderia.py limite unidade05 padrao  # volta ao padrão
```

Horários de funcionamento: `horarios_disponiveis.json` (período, duração de cada horário, `"fechado"` para o dia
todo). Máquinas: `MAQUINAS` em `app.py`.

## Arquivos de dados (fora do Git)

| Arquivo | Conteúdo |
|---------|----------|
| `agendamentos.json` | Reservas |
| `despesas.json` | Despesas lançadas na transparência |
| `cotas_lavanderia.json` | Limites próprios de algumas unidades |
| `historico_lavanderia.csv` | Registro de reservas, cancelamentos, despesas e trocas de senha |
| `backups/AAAA-MM-DD/` | Cópia diária dos dados (últimos 30 dias) |
