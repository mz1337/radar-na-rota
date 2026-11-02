# Radar na Rota

Mapa dos radares de fiscalização eletrônica do Brasil, com rota, lista de radares no caminho e alerta por voz, bipe e vibração ao se aproximar de cada um.

- **Mapa:** MapLibre + OpenFreeMap (vetorial, gira com a direção do carro, sem chave e sem custo).
- **Radares:** uma base única gerada toda semana a partir da ANTT, de prefeituras e DERs, do OpenStreetMap e das suas correções.
- **Celular:** publicado no GitHub Pages, instala como app e continua alertando com sinal fraco (a base e os trechos de mapa já vistos ficam guardados).

## O que tem em cada pasta

| Caminho | O que é |
|---|---|
| `app/` | O app: `index.html`, ícones, `manifest.webmanifest` e `sw.js` (funcionamento offline) |
| `pipeline/build_radares.py` | Gera `radares.json` juntando todas as fontes (só usa Python, sem instalar nada) |
| `pipeline/fontes_extras.json` | Listas oficiais extras (prefeituras, DERs). Dá para acrescentar outras |
| `pipeline/correcoes/` | Onde entram os arquivos de correção exportados pelo app |
| `workflow-github/radares.yml` | Atualiza a base toda segunda-feira e publica o site. No GitHub ele precisa ficar em `.github/workflows/radares.yml` (passo 4 abaixo) |

## Publicar no GitHub Pages (uma vez só)

1. Entre em [github.com](https://github.com) (crie uma conta grátis se não tiver).
2. Clique em **New repository**, dê o nome `radar-na-rota`, deixe **Public** e clique em **Create repository**.
3. Na página do repositório, clique em **uploading an existing file** e arraste as pastas `app` e `pipeline` e os arquivos `README.md` e `.gitignore`. Clique em **Commit changes**.
4. Crie o workflow: clique em **Add file > Create new file**, digite o nome `.github/workflows/radares.yml` (as barras criam as pastas), cole o conteúdo de `workflow-github/radares.yml` e clique em **Commit changes**.
5. Vá em **Settings > Pages** e, em **Build and deployment > Source**, escolha **GitHub Actions**.
6. Vá em **Actions**, abra **Atualizar radares e publicar** e clique em **Run workflow**. Leva de 2 a 5 minutos.
7. Ao terminar, o endereço aparece no resumo da execução: `https://SEU-USUARIO.github.io/radar-na-rota/`.

No celular, abra esse endereço no Chrome e toque em **Instalar o app** (ou menu ⋮ > **Instalar app**).

Depois disso, a base se atualiza sozinha toda segunda-feira. Para atualizar na hora, use **Run workflow** de novo.

## Rodar no computador (sem publicar)

No Prompt de Comando, dentro desta pasta:

```
python pipeline\build_radares.py
python -m http.server 8000 -d app
```

Depois abra `http://localhost:8000` no Chrome. A localização funciona em `localhost`.
Abrir o `index.html` com dois cliques também funciona, mas aí o app baixa os radares direto da ANTT e do OpenStreetMap, sem as listas extras.

## Corrigir radares

No app, toque num radar:
- **Não existe** some com ele do seu mapa;
- **Corrigir** muda o tipo ou o limite.

Para marcar um radar novo, toque no mapa e use **Marcar radar aqui**.

As correções ficam no seu aparelho. Para levar para a base de todos, use **Dados e alertas > Suas correções > Exportar correções** e coloque o arquivo em `pipeline/correcoes/` no repositório. Na próxima atualização elas entram na base.

## Acrescentar listas oficiais

Edite `pipeline/fontes_extras.json`. Tipos aceitos:

- `csv` ou `geojson`: um link direto para o arquivo;
- `ckan`: o portal de dados abertos e o nome do conjunto de dados (ex.: portais de prefeituras);
- `arcgis`: o link de uma camada de mapa (MapServer ou FeatureServer).

As colunas de latitude, longitude, velocidade e tipo são reconhecidas pelo nome. Se uma fonte falhar numa semana, a base reaproveita os radares dela da semana anterior.

## Limites que você precisa saber

- Os alertas só funcionam com o app aberto na tela. Em segundo plano o navegador pausa o GPS. No Android, use tela dividida com o Google Maps.
- As listas podem estar incompletas ou desatualizadas. A sinalização da via sempre prevalece.
- O sentido dos radares da ANTT ("crescente"/"decrescente") é estimado pelos quilômetros dos radares vizinhos. Em trechos com poucos radares ele pode ficar sem sentido, e aí o radar é avisado nos dois sentidos.
