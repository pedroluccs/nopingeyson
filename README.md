# NoPingeyson

Otimizador de rotas para jogos, escrito do zero: faz o mesmo *tipo* de coisa que
programas como o ExitLag (captura por jogo, medição de rotas, escolha da melhor,
multipath e failover, mais um "Boost" de sistema), usando só técnicas públicas
(WinDivert, UDP, HMAC). Não usa código, protocolo nem servidores de ninguém.

> **Importante:** o NoPingeyson só reduz ping/perda se você tiver **relays próprios**
> (VPSs baratas) bem posicionados. Sem relay, ele só mede. Veja "Relays" abaixo.

## Como funciona

```
 Seu PC (Windows)                    VPSs suas                      Servidor do jogo
 ┌──────────────┐   caminho 1   ┌──────────┐
 │ jogo.exe     │ ────────────► │ relay A  │──┐
 │   │ WinDivert│   caminho 2   └──────────┘  │  (saída: descarta cópias)
 │   ▼          │ ──► relay B ───────────────► relay A ─────────────────► 1 cópia
 │ NoPingeyson       │ ◄── respostas duplicadas, PC descarta repetidas ◄──────
 └──────────────┘
```

1. **Captura por aplicativo** (`nopingeyson/capture.py`): o WinDivert entrega cada
   pacote UDP que sai do PC. O NoPingeyson descobre o processo dono da porta e, se for
   um jogo da lista, desvia o pacote para o túnel; o resto segue intacto. As
   respostas são reinjetadas como se viessem do servidor do jogo.
2. **Medição** (`nopingeyson/tunnel.py`): sondas a cada 0,5 s em todos os caminhos
   (relay direto ou relay→relay) medem RTT, jitter e perda. Cada relay mede o ping
   dele até o servidor do jogo.
3. **Melhor rota**: nota = RTT até a saída + RTT saída→jogo + 2×jitter + perda.
   Quando surge um servidor novo, os primeiros pacotes esperam até 1,5 s enquanto
   mede; se a conexão direta for melhor, nada é desviado.
4. **Multipath**: cada pacote vai por até 3 caminhos que terminam no mesmo relay
   de saída (o servidor do jogo sempre vê um único IP). A saída repassa só a
   primeira cópia; na volta, o PC faz o mesmo. Perda em um caminho é coberta pelo
   outro ("Pacotes salvos" no painel).
5. **Failover**: caminho sem resposta por 1,5 s sai da lista sem mudar o IP de
   saída. Só se a saída inteira ficar 3 s muda, a saída é trocada (o jogo pode
   reconectar, porque o IP visto por ele muda).
6. **FPS Boost** (`nopingeyson/booster.py`): 11 ajustes (plano Alto
   desempenho, timer de 1 ms, prioridade alta, throttling de rede, Game DVR,
   Nagle, Modo de Jogo, efeitos visuais, apps em segundo plano, aceleração do
   mouse, agendamento de GPU). Tudo com backup e "Desfazer tudo".

## Funções (equivalentes às de um otimizador como o ExitLag)

| Função | Onde fica | Arquivo |
|---|---|---|
| Biblioteca de jogos com busca, gêneros e favoritos | Jogos | `games.py` |
| Por jogo: região, modo Automático/Manual, 1 a 4 conexões, botão **Aplicar rotas** | página do jogo | `tunnel.py`, `engine.py` |
| Gráfico de ping em tempo real, ping direto x otimizado, pacotes salvos | página do jogo | `ui/index.html` |
| Multipath (duplicação de pacotes) e failover | automático | `tunnel.py`, `relay.py` |
| Traffic Shaper: limita download/upload dos outros programas, com exceções | Traffic Shaper | `shaper.py` |
| Multi Internet: usa 2 conexões (cabo + Wi‑Fi, fibra + 4G) ao mesmo tempo | Multi Internet | `tunnel.py`, `netmon.py` |
| FPS Boost | FPS Boost | `booster.py` |
| Pedir jogo novo: monitora a atividade de rede e adiciona o processo | Pedir jogo novo | `netmon.py` |
| Diagnóstico: teste de ping, teste de velocidade, monitor do PC | Diagnóstico | `netmon.py` |

A interface segue o mesmo fluxo de uso (escolher o jogo → ajustar → aplicar
rotas), com visual e marca próprios do NoPingeyson. Não há sistema de conta/login:
tudo roda local, nos seus servidores.

O protocolo do túnel está documentado em `nopingeyson/protocol.py`. Todo pacote é
assinado com HMAC-SHA256 usando uma chave sua, então o relay não vira proxy
aberto para terceiros. O conteúdo **não é criptografado** (o tráfego de jogo já
segue assim pela internet normalmente).

## Baixar e instalar

Baixe o **NoPingeyson-Setup-x.y.z-x64.exe** na aba
[Releases](https://github.com/pedroluccs/nopingeyson/releases), rode e siga o
instalador (cria atalhos e desinstalador; ao desinstalar, o FPS Boost é desfeito).
O programa pede permissão de Administrador ao abrir.

## Pronto para usar (v0.3)

* **Detecta sozinho** os jogos e apps instalados (Steam, Epic, Riot, Battle.net,
  Ubisoft, EA e registro do Windows) e mostra o **ícone de verdade** de cada um
  (ícone do .exe ou capa da Steam, extraídos no seu PC).
* **Otimização inteligente:** quando um jogo abre, ele entra em Conexões e é
  otimizado sem você clicar em nada.
* Telas: Início, Conexões, Biblioteca (categorias e índice A–Z), Servidores,
  Analisador de rede (tempo real, rastreio de rota, ping para regiões,
  velocidade), PC Boost (presets, Limpeza de RAM, DNS Optimizer), Multi
  Internet, Limitador de tráfego, Configurações (iniciar com o Windows, bandeja).

### Servidores embutidos

A redução de ping precisa de servidores (relays) perto do servidor do jogo. Para
o usuário final não configurar nada, o dono do projeto coloca os servidores em
`nopingeyson/servers.json` **antes de gerar o instalador**:

```json
{"key": "CHAVE_HEX", "servers": [{"name": "SP-1", "region": "São Paulo", "host": "203.0.113.10", "port": 7000}]}
```

Sem servidores, o NoPingeyson funciona normalmente (PC Boost, limitador,
analisador, DNS, RAM), mas o ping do jogo não muda: o jogo segue pela rota normal.

### Servidor grátis na Oracle Cloud (tenta até ter vaga)

1. Crie uma conta na Oracle Cloud escolhendo **São Paulo** como região principal
   (ela é definitiva; o plano Always Free só vale na região principal).
2. No console: ícone do perfil → **Meu perfil** → **Chaves de API** → **Adicionar
   chave de API** → **Gerar par de chaves** → baixe a chave privada → **Adicionar**.
   Copie o texto da "Visualização do arquivo de configuração" para
   `C:\Users\SEU_USUARIO\.oci\config` e troque `key_file=` pelo caminho da chave baixada.
3. Na pasta do projeto:

```bash
pip install oci
```

```bash
python -m nopingeyson oracle
```

O comando cria a rede, libera a porta UDP 7000 e tenta criar a VM gratuita
(Ampere A1 ou AMD Micro) em todas as zonas, repetindo a cada 60 s até a Oracle
liberar vaga. A VM se instala sozinha; quando o relay responde, o servidor é
salvo na configuração e o comando mostra o JSON para o segredo
`NOPINGEYSON_SERVERS` do GitHub (Settings → Secrets and variables → Actions),
que embute o servidor nos próximos instaladores.

## Anti-cheat: o que esperar

O NoPingeyson foi feito para **não mexer no jogo**: não lê nem escreve na memória
do jogo, não injeta DLL, não desenha overlay e não altera arquivos do jogo. Ele
só trabalha com os pacotes de rede, fora do processo do jogo.

Mesmo assim, **não é possível garantir** que nenhum anti-cheat vai reclamar:

* o desvio de tráfego usa o driver **WinDivert**. Ele é assinado e usado por
  muitos programas, mas alguns anti-cheats em modo kernel (Vanguard, FACEIT,
  ESEA e outros) podem bloquear o driver ou recusar abrir o jogo com ele
  carregado. Programas comerciais como o ExitLag usam driver próprio assinado e
  acordos com os fabricantes de anti-cheat, coisa que um projeto pessoal não tem;
* a opção "Prioridade alta para o jogo" abre o processo do jogo para mudar a
  prioridade. Vem **desligada**; deixe assim em jogos com anti-cheat em kernel;
* o .exe não tem assinatura digital paga, então o Windows SmartScreen pode
  avisar na primeira vez.

Se um jogo reclamar, feche o NoPingeyson (o driver é descarregado) e abra o jogo
de novo.

## Gerar o .exe e o instalador

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Precisa de `pip install -r requirements.txt pyinstaller` e do Inno Setup 6.
Saída: `build\dist\NoPingeyson\NoPingeyson.exe` e
`build\installer\NoPingeyson-Setup-*.exe`. Ao enviar uma tag `v*` para o GitHub,
o workflow `.github/workflows/release.yml` faz o mesmo build e publica o
instalador na aba Releases.

## Instalação para desenvolvimento (Windows)

Requer Python 3.10+.

```bash
pip install -r requirements.txt
```

```bash
python -m nopingeyson keygen
```

Guarde a chave gerada: ela vai no cliente e em todos os relays. Depois abra a
interface **como Administrador** (o WinDivert exige):

```bash
python -m nopingeyson
```

Em **Configurações**, cole a chave e cadastre seus servidores com uma região.
Em **Jogos**, abra o jogo, escolha região/modo/conexões e clique em **Aplicar
rotas**. Em **Minhas rotas** dá para só medir os servidores. Também dá para usar sem interface:
`python -m nopingeyson medir` ou `python -m nopingeyson run` (lê `config.json`; veja
`config.example.json`).

## Relays (VPS)

Qualquer VPS Linux com IPv4 público serve (1 vCPU / 512 MB sobra). Dicas:

* Para o relay de **saída**, escolha a mesma cidade/datacenter dos servidores do
  jogo (ex.: São Paulo para Valorant/CS2/LoL BR).
* Para relays de **entrada**, teste provedores diferentes: às vezes a rota do seu
  provedor até a VPS B e de B até A é melhor que até A direto. É aí que rotas
  de 2 saltos ajudam.
* Libere a porta UDP (padrão 7000) no firewall da VPS e no painel do provedor.

Instalação (Debian/Ubuntu), copiando a pasta `nopingeyson` e o script:

```bash
scp -r nopingeyson deploy/install-relay.sh root@IP_DA_VPS:/root/
```

```bash
ssh root@IP_DA_VPS "cd /root && bash install-relay.sh SUA_CHAVE_HEX 7000"
```

Ou rodar na mão: `NOPINGEYSON_KEY=... python3 -m nopingeyson.relay --port 7000`.

## Testes

```bash
python -m unittest discover -s tests -v
```

Sobe 3 relays em loopback (dois com 25% de perda simulada), um servidor de eco e
confere: escolha de rota, multipath sem perda e sem duplicatas para o "jogo", e
failover quando o relay de saída cai.

## Limitações e cuidados

* **Só IPv4 e UDP** nesta versão. Tráfego TCP do jogo (login, loja, chat) segue
  normal; a partida em si costuma ser UDP.
* **Anti-cheat**: alguns anti-cheats (ex.: Vanguard, FACEIT) podem bloquear ou
  desconfiar de drivers de captura como o WinDivert. Teste com cuidado; use por
  sua conta e risco.
* O Traffic Shaper passa todo o tráfego do PC pelo Python: use só quando
  precisar (downloads pesados ao mesmo tempo que o jogo).
* A captura passa todo UDP de saída pelo Python; para muitos jogos isso é
  irrelevante, mas um downloader UDP pesado ao mesmo tempo pode sentir.
* Pacotes muito grandes ganham ~60 bytes de cabeçalho no túnel e podem ser
  fragmentados.
* Os ajustes "Nagle" e "throttling" mexem no registro (HKLM) e podem exigir
  reiniciar o PC para valer. O backup fica em `%APPDATA%\NoPingeyson\backup.json`.

## Estrutura

```
nopingeyson/protocol.py   formato dos pacotes, HMAC, janela anti-duplicata
nopingeyson/relay.py      servidor relay (sem dependências)
nopingeyson/tunnel.py     medição, escolha de rota, multipath, failover
nopingeyson/capture.py    WinDivert: captura por processo e reinjeção
nopingeyson/booster.py    ajustes de sistema reversíveis
nopingeyson/engine.py      junta tudo; configuração; rotas por jogo
nopingeyson/games.py       biblioteca de jogos
nopingeyson/shaper.py      Traffic Shaper
nopingeyson/netmon.py      Multi Internet, monitor de rede, diagnóstico
nopingeyson/gui.py + ui/  interface (pywebview)
deploy/              instalação do relay como serviço systemd
tests/               teste ponta a ponta em loopback
```
