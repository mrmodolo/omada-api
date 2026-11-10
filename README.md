# omada_ifstat

API HTTP mínima que expõe o status e as estatísticas das interfaces WAN do
roteador **TP-Link Omada ER605** (testado no ER605 v2.20).

Os dados vêm das mesmas fontes usadas pela interface web do roteador:

| Tela do roteador | Endpoint interno do firmware |
|---|---|
| System Status → WAN IPv4 | `/admin/interface?form=status2` |
| Interface Statistics → Statistics List | `/admin/ifstat?form=list` |

Usa só a biblioteca padrão do Python 3, sem dependências.

## Uso

```sh
export OMADA_USER=admin
export OMADA_PASS='sua_senha'

python3 omada_ifstat.py            # sobe a API em http://127.0.0.1:8080
python3 omada_ifstat.py --once     # imprime status + estatísticas por WAN e sai
python3 omada_ifstat.py --table    # mesmo conteúdo, em tabelas para leitura humana
python3 omada_ifstat.py --raw      # imprime o JSON bruto do roteador (depuração)
```

### Saída `--table`

```text
WAN Status
Interface  Connection Type  Status   IP Address     Subnet Mask    MAC Address        Gateway        Primary DNS    Online Check
---------  ---------------  -------  -------------  -------------  -----------------  -------------  -------------  ------------
WAN1       Dynamic IP       Link Up  192.168.0.21   255.255.255.0  DC-62-79-48-1C-74  192.168.0.1    192.168.0.1    Online
WAN/LAN2   Dynamic IP       Link Up  192.168.100.2  255.255.255.0  DC-62-79-48-1C-75  192.168.100.1  192.168.100.1  Online

WAN Statistics
Interface  TX KB/s  RX KB/s  TX Pkt/s  RX Pkt/s  Total TX  Total RX  TX Packets  RX Packets
---------  -------  -------  --------  --------  --------  --------  ----------  ----------
WAN1            18       73        48        75  205.1 MB  940.3 MB      523582     1021575
WAN/LAN2         1        1         3         3   76.2 MB  339.3 MB      228254      472686
```

Os totais de bytes aparecem em unidades de base 1024 (KB, MB, GB, TB). Se
uma WAN não tiver estatísticas, os valores dela aparecem como `-`.

Para acompanhar ao vivo: `watch -n 10 python3 omada_ifstat.py --table`. Cada
execução faz login e logout de novo e, como o ER605 aceita uma sessão por
vez, derruba quem estiver na interface web. Num intervalo curto, prefira a
API em modo servidor.

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `OMADA_URL` | `https://omada.modolo.net.br` | Endereço do roteador |
| `OMADA_USER` | `admin` | Usuário da interface web |
| `OMADA_PASS` | — | Senha (obrigatória) |
| `OMADA_VERIFY_TLS` | `0` | `1` para validar o certificado HTTPS |
| `LISTEN_HOST` | `127.0.0.1` | Endereço em que a API escuta |
| `LISTEN_PORT` | `8080` | Porta da API |
| `OMADA_LOGIN_BACKOFF` | `60` | Segundos de espera após um login recusado (mínimo de 300 s se o roteador bloquear com 709) |
| `OMADA_YIELD_SECONDS` | `120` | Segundos de espera antes de relogar depois que alguém entra pela interface web (711) ou há conflito de usuário (710) |

## Endpoints

| Endpoint | Conteúdo |
|---|---|
| `GET /wan` | status + estatísticas, por WAN |
| `GET /status` | status das WANs (tabela "WAN IPv4") |
| `GET /ifstat` | estatísticas de todas as interfaces, inclusive LAN |

Todos aceitam um filtro de interface: `/wan/wan1`, `/status/wan2`,
`/ifstat/lan`. O filtro compara com `zone` (`WAN1`, `WAN2`, `LAN1`) ou com
`interface` (`WAN1`, `WAN/LAN2`, `LAN`), ignorando maiúsculas e símbolos;
por isso `wan2` e `wanlan2` encontram a mesma WAN.

| HTTP | Quando |
|---|---|
| `200` | Sucesso |
| `404` | Endpoint ou interface inexistente |
| `502` | Falha ao falar com o roteador (rede, erro do firmware) |
| `503` | Esperando para relogar (veja [Sessão e reautenticação](#sessão-e-reautenticação)); traz o cabeçalho `Retry-After` |

```sh
curl -s http://127.0.0.1:8080/wan/wan1
```

### Exemplo de resposta (`GET /wan/wan1`)

```json
[
  {
    "zone": "WAN1",
    "interface": "WAN1",
    "status": {
      "connection_type": "Dynamic IP",
      "connection_status": "Link Up",
      "ip_address": "192.168.0.21",
      "subnet_mask": "255.255.255.0",
      "mac_address": "DC-62-79-48-1C-74",
      "default_gateway": "192.168.0.1",
      "primary_dns": "192.168.0.1",
      "online_check": "Online"
    },
    "statistics": {
      "tx_rate_kbps": 18,
      "rx_rate_kbps": 73,
      "tx_packet_rate_pps": 48,
      "rx_packet_rate_pps": 75,
      "total_tx_bytes": 215062075,
      "total_rx_bytes": 985985295,
      "total_tx_packets": 523582,
      "total_rx_packets": 1021575
    }
  }
]
```

## Campos

### `status`

| Campo | Coluna na tela | Valores |
|---|---|---|
| `connection_type` | Connection Type | `Dynamic IP`, `Static IP`, `PPPoE`, `L2TP`, `PPTP`... |
| `connection_status` | Connection Status | `Link Up` / `Link Down` |
| `ip_address` | IP Address | `0.0.0.0` quando ausente |
| `subnet_mask` | Subnet Mask | `0.0.0.0` quando ausente |
| `mac_address` | MAC Address | `00-00-00-00-00-00` quando ausente |
| `default_gateway` | Default Gateway | `0.0.0.0` quando ausente |
| `primary_dns` | Primary DNS | `0.0.0.0` quando ausente |
| `online_check` | Online Check | `Online` / `Offline` |

### `statistics`

| Campo | Coluna na tela | Observação |
|---|---|---|
| `tx_rate_kbps` / `rx_rate_kbps` | TX / RX Rate (KB/s) | **kilobytes** por segundo (o firmware chama de `tx_bps`) |
| `tx_packet_rate_pps` / `rx_packet_rate_pps` | TX / RX Packet Rate (Pkt/s) | pacotes por segundo |
| `total_tx_bytes` / `total_rx_bytes` | Total TX / RX Bytes | contador acumulado |
| `total_tx_packets` / `total_rx_packets` | Total TX / RX Packets | contador acumulado |

Os campos `total_*` só crescem. Para medir o consumo num período, calcule a
diferença entre duas leituras.

## Como funciona o login

O firmware não tem API documentada. O script reproduz o que a interface web
faz:

1. Lê a chave pública RSA (`n`, `e`) em `/login?form=login` e o `uptime` em
   `/locale?form=lang`.
2. Cifra `senha_uptime` com RSA **sem padding** (função `nopadding` do
   `encrypt.js` do roteador).
3. Envia o login e guarda o token `stok` e o cookie de sessão. Sem o cookie,
   o roteador responde `704`.
4. Chama `/cgi-bin/luci/;stok=<token>/admin/...`.

O firmware aceita dois formatos de requisição (`data={"method":...}` ou
`operation=read`), e cada endpoint só responde a um deles; o outro devolve
`1014`. O script tenta os dois.

## Sessão e reautenticação

O login acontece na primeira requisição, e a mesma sessão é reaproveitada
nas seguintes. Quando a sessão cai, a API se recupera sozinha:

| Situação | Comportamento |
|---|---|
| Sessão expirada ou token inválido (`703`, `704`), HTTP `401`/`403` ou resposta que não é JSON (ex.: página de login) | Refaz o login e repete a chamada, de forma transparente (HTTP 200) |
| Sessão encerrada pelo roteador (`711`): alguém fez login, por exemplo pela interface web | **Não** reloga na hora: espera `OMADA_YIELD_SECONDS` e responde 503 nesse intervalo |
| Login recusado (`700`) | Espera `OMADA_LOGIN_BACKOFF` antes de tentar de novo e responde 503 nesse intervalo |
| Muitas tentativas (`709`) ou conflito de usuário (`710`) | Igual, com espera maior |
| Outros erros do firmware | Responde 502 sem relogar |

O ER605 aceita **uma sessão por vez** (confirmado no ER605 v2.20): um novo
login encerra a sessão anterior, que passa a receber `711`. Se a API relogasse
imediatamente, derrubaria quem está usando a interface web; por isso ela
espera. Durante essa espera, e durante o backoff de login, a API não faz
nenhuma chamada ao roteador.

As esperas existem para não esgotar as tentativas de login. Com senha errada,
um cliente que consulta a API a cada poucos segundos bloquearia a conta do
roteador.

Ao encerrar (Ctrl+C ou `SIGTERM`, como no `systemctl stop`), e ao fim dos
modos `--once`, `--table` e `--raw`, o script faz logout para liberar a sessão.
Os eventos de sessão vão para o stderr com data e hora:

```text
2026-11-09 12:05:08 login efetuado
2026-11-09 12:05:08 sessão expirada (704); refazendo login
2026-11-09 12:05:08 sessão encerrada pelo roteador (711): outro login ou logout; novo login em 120s
2026-11-09 12:05:39 logout efetuado
```

## Códigos de erro do firmware

| Código | Significado |
|---|---|
| `700` | Login falhou (usuário ou senha) |
| `703` / `704` | Sessão expirada |
| `709` | Bloqueado por excesso de tentativas |
| `710` | Conflito de usuário (outra sessão ativa) |
| `711` | Sessão encerrada por outro login ou por logout |
| `1014` | Formato de requisição não aceito pelo endpoint |

## Segurança

- A verificação do certificado HTTPS fica **desligada** por padrão, porque o
  certificado do roteador não corresponde ao hostname. Isso deixa a conexão
  exposta a man-in-the-middle; ative `OMADA_VERIFY_TLS=1` se instalar um
  certificado válido.
- A API **não tem autenticação**. Com `LISTEN_HOST=0.0.0.0`, qualquer pessoa
  na rede consegue ler IPs, MACs e tráfego das WANs. Só exponha em rede
  confiável.
- Variáveis de proxy (`HTTP_PROXY`, `HTTPS_PROXY`...) são ignoradas: o script
  fala direto com o roteador.
- Não deixe a senha em arquivos versionados; use variáveis de ambiente.
