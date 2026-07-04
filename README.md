![MainPrint](https://i.imgur.com/Il6u24A.png)

# X Media Scraper

Baixe todas as fotos e vídeos da página `/media` de qualquer perfil do X (Twitter) na **máxima qualidade disponível**.

---

## ✨ Recursos

- 🖼 **Fotos** na resolução original (`?name=orig`)
- 🎬 **Vídeos** na maior taxa de bits disponível (MP4)
- ⚡ **Downloads simultâneos** com múltiplas threads (configurável de 1 a 32 threads)
- 📊 Barras de progresso detalhadas e tabela de resumo
- 🔄 Paginação automática (suporta perfis com centenas ou milhares de mídias)
- 🛡 Tratamento automático de *rate limit* com novas tentativas
- 📁 Ignora arquivos que já foram baixados
- 🚀 Descoberta automática dos endpoints GraphQL do X
- 🔒 Autenticação utilizando sua sessão do navegador

---

# 🚀 Auto GraphQL Discovery

Um dos maiores problemas dos scrapers para X (Twitter) é depender de **IDs GraphQL fixos**.

Quando o X altera esses IDs, a maioria dos scrapers simplesmente para de funcionar até que alguém atualize o código manualmente.

Este projeto resolve esse problema automaticamente.

Antes de iniciar qualquer download, o scraper:

- 🔍 Acessa a página pública do X
- 📦 Localiza os bundles JavaScript mais recentes
- 🧠 Extrai automaticamente os `queryId` utilizados pelo próprio X
- ⚙️ Atualiza todos os endpoints GraphQL em tempo real

### Benefícios

✅ Não depende de IDs GraphQL fixos

✅ Muito mais resistente às atualizações do X

✅ Elimina a necessidade de atualizar manualmente os endpoints

Esse mecanismo torna o projeto muito mais confiável e reduz significativamente a manutenção necessária ao longo do tempo.

---

# Instalação

Clone o repositório:

```bash
git clone https://github.com/Apolinario-coder/x-media-scraper.git
cd x-media-scraper
```

Instale as dependências:

```bash
pip install -r requirements.txt
```

---

# ▶️ Como usar

Execute o programa:

```bash
python x_scraper.py
```

O programa irá:

1. Abrir uma janela do navegador para que você faça login no X.
2. Descobrir automaticamente os endpoints GraphQL mais recentes.
3. Carregar toda a página `/media` do perfil informado.
4. Coletar todas as fotos e vídeos disponíveis.
5. Baixar os arquivos na maior qualidade possível.

---

# ⚙️ Como funciona

O processo acontece da seguinte forma:

1. Autentica utilizando sua sessão do navegador.
2. Descobre automaticamente os endpoints GraphQL atuais do X.
3. Converte o nome de usuário para o ID interno da conta.
4. Percorre automaticamente toda a timeline `/media`.
5. Extrai as URLs das fotos e vídeos na maior qualidade disponível.
6. Baixa todos os arquivos utilizando múltiplas threads.

---

# 📁 Estrutura dos downloads

```
downloads/
└── usuario/
    ├── photos/
    └── videos/
```

---

# ⚡ Performance

O número de threads pode ser configurado entre **1 e 32**, permitindo aproveitar melhor sua conexão de internet.

Também são utilizados:

- Download paralelo
- Reutilização de conexões HTTP
- Retry automático
- Continuação de downloads interrompidos
- Verificação para evitar baixar arquivos duplicados

---

# 🛠 Tecnologias

- Python 3
- Selenium
- Requests
- GraphQL
- BeautifulSoup
- Rich
- ThreadPoolExecutor

---

# 📄 Licença

Este projeto é distribuído sob a licença MIT.

Sinta-se à vontade para utilizar, modificar e contribuir.
