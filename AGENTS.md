# Diretrizes e Regras do Agente (AGENTS.md)

Este documento define as regras estritas sobre o que **DEVE** e o que **NÃO PODE** ser feito neste repositório. Toda vez que uma alteração for solicitada ao assistente/agente de IA, estas instruções devem ser consultadas e seguidas rigorosamente.

---

## 🚫 O QUE NÃO PODE SER FEITO (Proibições Críticas)

1. **Repositório Público - Sigilo e Anonimização:**
   - **NUNCA** colocar nomes reais de clientes, provedores, empresas ou indivíduos em testes, código, documentação, exemplos, commits ou comentários.
   - **NUNCA** expor endereços IP reais (públicos ou privados de clientes) fornecidos nos prompts ou saídas de comandos. Sempre utilize faixas reservadas para documentação e testes ([RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737): `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`; [RFC 1918](https://datatracker.ietf.org/doc/html/rfc1918): `10.0.0.1`, `192.168.1.1`; ou nomes como `CLIENTE-X`, `CLIENTE-Y`, `MK-ROUTER-TEST`).
   - **NUNCA** commitar tokens, senhas, chaves de API, credenciais SSH ou dados do arquivo `.env`.

2. **Segurança de Acesso e Redes:**
   - **NUNCA** tentar conectar via SSH em equipamentos ou endereços IP de produção fornecidos pelo usuário em mensagens/erros, a menos que haja solicitação explícita para conexão direta.

---

##  O QUE DEVE SER FEITO (Boas Práticas e Fluxo Obrigatório)

1. **Leitura Prévia:**
   - Ler este arquivo (`AGENTS.md`) antes de realizar alterações no projeto para garantir conformidade contínua.

2. **Testes Automatizados:**
   - Sempre executar a suite de testes (`python3 -m pytest`) após qualquer alteração no código para validar que nada foi quebrado.
   - Ao adicionar novas funcionalidades ou corrigir bugs, adicionar testes unitários correspondentes em `tests/` usando mocks e dados genéricos/fictícios.

3. **Atualização da Documentação (`README.md`):**
   - Sempre que uma funcionalidade nova, flag de linha de comando (`--flag`) ou parâmetro de configuração for criado ou alterado, atualizar o `README.md`.
   - Manter a estrutura, padrão de tabelas, exemplos numerados e estilo visual já adotados na documentação existente.

4. **Integridade do Código:**
   - Manter docstrings, comentários explicativos e padrões de tipagem e tratamento de erros do projeto.
