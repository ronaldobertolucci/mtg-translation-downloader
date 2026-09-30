# mtg-translation-downloader

Job de ETL em Python 3.12+, sem dependências externas. Descobre o bulk `all_cards`
no Scryfall e baixa para disco em blocos de 1 MiB. Usa `jsonl_download_uri`
(atualmente JSONL comprimido com gzip), com fallback para o antigo `download_uri`.
A leitura e descompressão são incrementais, sem expandir o arquivo inteiro em
disco ou memória. Também aceita arrays JSON antigos, lidos em blocos de 64 KiB.
Apenas impressões elegíveis e os campos usados pelo merge
ficam em memória, agrupados por `oracle_id`. A memória cresce com as impressões
portuguesas aceitas, não com o arquivo inteiro. Reserve espaço em disco para o
bulk completo; `--work-dir` permite escolher o volume temporário.

## Executar

```bash
# Baixar, consolidar e conferir os payloads, sem ingestão:
python3 -m mtg_translation_downloader --dry-run --output translations.jsonl

# Baixar, consolidar e enviar para a API existente:
python3 -m mtg_translation_downloader --output translations.jsonl

# Reutilizar um bulk local:
python3 -m mtg_translation_downloader --input /dados/all-cards.json --output translations.jsonl

# O formato é detectado pelo conteúdo; JSONL gzip também é aceito:
python3 -m mtg_translation_downloader --input /dados/all-cards.jsonl.gz --output translations.jsonl
```

O endpoint padrão é `http://localhost:8002/translations`, confirmado pelo OpenAPI
do `mtg-card-manager`. O job envia um `POST` por `oracle_id`, no formato da
especificação (campos na raiz ou `card_faces` com `face_index`). Configure a URL
completa com `--manager-url` ou `MTG_MANAGER_URL`. A API consultada não declara
autenticação; se necessário, `MTG_MANAGER_TOKEN` envia um Bearer token apenas
ao manager. `--timeout` define o timeout de rede por operação (padrão: 120 s).

## Regras de transformação

- Aceita somente `lang=pt`, descarta `promo=true` e `textless=true`, e permite
  apenas `core`, `expansion`, `masters`, `commander`, `draft_innovation`, `starter`.
- Ordena as impressões por `released_at` decrescente; empates usam o `id` da
  impressão em ordem decrescente para manter o resultado determinístico.
- Seleciona a impressão mais recente com `printed_name`, `printed_type_line` e
  `printed_text` preenchidos. Nome, tipo e regras vêm juntos dessa impressão,
  sem copiar campos originais em inglês. Valores só com espaços não são preenchidos.
- `printed_text` pode estar ausente ou vazio quando `oracle_text` também estiver
  ausente ou vazio (carta sem regras); nesse caso, envia `oracle_text: ""`.
  O texto original serve apenas para essa verificação, nunca como fallback.
- Sem uma impressão com tradução completa, a carta não é enviada. O log informa
  quantos `oracle_id` foram descartados por esse motivo.
- Após escolher a tradução, flavor é o primeiro texto não vazio de todo o
  histórico elegível, do mais novo para o mais antigo, incluindo impressões
  com tradução incompleta. Espaços em branco não contam como texto.
- Compatibilidade com a fonte real: os [tipos oficiais do Scryfall](https://github.com/scryfall/api-types/blob/main/src/objects/Card/CardFields.ts)
  expõem `flavor_text`, não `printed_flavor_text`. Em cada impressão, o job prefere
  `printed_flavor_text` quando preenchido e aceita `flavor_text` como fallback.
  A seleção de idioma já foi aplicada à impressão inteira.
- Cartas com `card_faces` exigem tradução completa em todas as faces da mesma
  impressão. A busca do flavor ocorre por índice, para cada face separadamente.
  Textos da raiz não substituem os textos das faces.
- Na ausência de flavor, envia `null`. Uma impressão elegível sem `oracle_id`,
  com data inválida ou faces malformadas interrompe o processamento.

## Falhas e operação

O job termina a leitura e materializa os payloads antes do primeiro POST. JSON
truncado ou inválido interrompe a execução sem ingestão. Há um limite de 8 MiB
de caracteres por objeto para evitar crescimento ilimitado do buffer. O bulk
temporário e downloads incompletos são removidos ao terminar, inclusive em erro.

Qualquer falha de ingestão encerra o job com código 1 e informa o `oracle_id` e
quantas entregas foram confirmadas. O arquivo `--output`, quando solicitado,
permanece disponível. Não há transação global nem retries automáticos de POST:
uma falha pode deixar carga parcial e um timeout pode ocorrer após o servidor
ter gravado o registro. O endpoint é de criação; o job não presume upsert nem
sobrescreve traduções existentes via PATCH. Antes de reexecutar uma carga parcial,
verifique os registros já criados e o comportamento de duplicatas do manager.

## Container

```bash
docker build -t mtg-translation-downloader .
# Linux: a rede do host permite alcançar a API em localhost:8002.
docker run --rm --network host mtg-translation-downloader
```

Em uma rede Docker compartilhada, use `MTG_MANAGER_URL` com o nome do serviço e
a porta interna do manager. Para guardar o JSONL fora do container, monte um
volume e passe `--output /saida/translations.jsonl`.

## Testes

```bash
python3 -m unittest discover -s tests -v
```

Testes cobrem parsing fragmentado, filtros, recência, completude da tradução, merge por face,
download, payloads HTTP e falhas. Chamadas de rede são simuladas; os testes não
alteram a base real do manager nem baixam o bulk completo.
