# SQL, Banco de Dados e Integracao — Base de Conhecimento maxPedido/Winthor

> Documento consolidado para ingestao em banco vetorial (RAG).
> Fontes: SQLs_GERAL_ORGANIZADO.md, Selects.pdf, rotinas x tabelas.pdf, Layout Integracao maxPedido.pdf

**Palavras-chave**: SQL, Oracle, Winthor, integracao, pedido, nuvem, sincronizacao, extrator, banco de dados, tabelas, rotinas
**Sistema**: maxPedido / Winthor ERP
**Area**: SQL / Banco de Dados / Integracao

---

## 1. ATUALIZID PADRAO

Para forcar reenvio de registros para os aparelhos, utilize:

```sql
SET ATUALIZID = '-9999999999'
```

---

## 2. STATUS DOS PEDIDOS NA MXSINTEGRACAOPEDIDOS

| Status | Descricao |
|--------|-----------|
| 0 | RecebidoPeloServer |
| 1 | EnviadoParaApi |
| 2 | EnviadoParaErp |
| 3 | RecebidoPeloErp |
| 4 | ProcessadoPeloErp |
| 5 | ErroProcessamentoErp |
| 6 | PedidoBloqueadoEnvioErp |
| 7 | PedidoBloqueadoCancelado |
| 8 | Pedido Pendente Autorizacao |
| 9 | Pedido Autorizado |
| 10 | Pedido Negado |
| 11 | JobWinthor |

---

## 3. PEDIDOS NUVEM

### Registros pendentes de integracao

```sql
SELECT tabela, COUNT(1) FROM maxsolucoes.pcmxsintegracao
WHERE status = '-1' GROUP BY tabela ORDER BY COUNT(1) DESC;
```

### Pedidos pendentes por status

```sql
SELECT * FROM mxsintegracaopedido
WHERE dtinclusao IS NOT NULL AND status IN (0,1,2,5);
```

### Pedidos processados do dia com critica

```sql
SELECT * FROM mxsintegracaopedido
WHERE status = 4 AND TRUNC(data) = TRUNC(sysdate)
AND critica NOT LIKE '%{}%';
```

### IDs de pedidos nao finalizados

```sql
SELECT id_pedido || ',' FROM MXSINTEGRACAOPEDIDO
WHERE STATUS NOT IN (4,6,7) ORDER BY DTINCLUSAO ASC;
```

---

## 4. SINCRONIZACAO

### Status da sincronizacao de clientes

```sql
SELECT STATUS, COUNT(1) FROM MXSINTEGRACAOCLIENTE
WHERE TRUNC(DATA) = TRUNC(SYSDATE) GROUP BY STATUS;
```

### Clientes duplicados por CNPJ

```sql
SELECT CLIENTE, COUNT(*) FROM PCCLIENTFV
WHERE CGCENT = '33.893.227/0001-62'
AND TRUNC(DTINCLUSAO) = TRUNC(SYSDATE) GROUP BY CLIENTE;
```

### Alteracoes de clientes nos ultimos 3 dias

```sql
SELECT CLIENTE, COUNT(*) AS ALTERACOES
FROM PCCLIENTFV
WHERE OBSERVACAO_PC = 'CLIENTE ALTERADO COM SUCESSO'
AND TRUNC(DTINCLUSAO) >= TRUNC(SYSDATE) - 3
GROUP BY CLIENTE
ORDER BY COUNT(*) DESC;
```

---

## 5. CONEXOES E APARELHOS

### Log de conexoes dos aparelhos

```sql
SELECT CODUSUARIO, DTTERMINOCONEXAO, ERROR, APPVERSION
FROM MXSAPARELHOSCONNLOG ORDER BY DTTERMINOCONEXAO DESC;
```

### Registros pendentes na pcmxsintegracao

```sql
SELECT tabela, COUNT(1) FROM pcmxsintegracao
WHERE status = '-1' GROUP BY tabela ORDER BY COUNT(1) DESC;
```

### Usuarios conectados em periodo especifico

```sql
SELECT DISTINCT(mxsaparelhosconnlog.codusuario), mxsusuarios.codusur, appversion, dtinicioconexao
FROM mxsaparelhosconnlog
LEFT JOIN mxsusuarios ON mxsusuarios.codusuario = mxsaparelhosconnlog.codusuario
WHERE DTINICIOCONEXAO BETWEEN TO_DATE('30/04/2019 01:00:01', 'DD/MM/YYYY HH24:MI:SS')
AND TO_DATE('30/04/2019 23:49:01', 'DD/MM/YYYY HH24:MI:SS')
ORDER BY mxsaparelhosconnlog.dtinicioconexao DESC;
```

### Aparelhos de usuario especifico

```sql
SELECT DISTINCT DEVICEINSTALLKEY, MARCA_APARELHO, MODELO_APARELHO
FROM MXSAPARELHOSCONNLOG
WHERE CODUSUARIO = 14235
AND (DTINICIOCONEXAO) > TRUNC(SYSDATE) - 1;
```

### Relacao completa de aparelhos conectados

```sql
SELECT DISTINCT A.DEVICEINSTALLKEY, A.MARCA_APARELHO, A.MODELO_APARELHO,
  A.CODUSUARIO, U.NOME, U.LOGIN
FROM MXSAPARELHOSCONNLOG A
INNER JOIN MXSUSUARIOS U ON A.CODUSUARIO = U.CODUSUARIO
WHERE (DTINICIOCONEXAO) > TRUNC(SYSDATE) - 1
ORDER BY NOME;
```

---

## 6. CONSULTAR SUPERVISORES

```sql
-- Supervisores
SELECT * FROM mxsusuari WHERE codusur = codsupervisor;

-- RCA por equipe de supervisores
SELECT * FROM mxsusuari WHERE CODSUPERVISOR = :cod_supervisor;
```

---

## 7. QUANTIDADE DE RCA QUE JA CONECTARAM

```sql
SELECT DISTINCT codusuario FROM mxsaparelhosconnlog
WHERE DTINICIOCONEXAO BETWEEN TO_DATE('01/01/2019 01:00:01', 'DD/MM/YYYY HH24:MI:SS')
AND TO_DATE('17/04/2019 23:49:01', 'DD/MM/YYYY HH24:MI:SS');
```

---

## 8. RCAS QUE JA PASSARAM PEDIDOS

```sql
SELECT DISTINCT codusur FROM mxsintegracaopedido
WHERE DATA BETWEEN TO_DATE('01/01/2019 01:00:01', 'DD/MM/YYYY HH24:MI:SS')
AND TO_DATE('05/04/2019 23:49:01', 'DD/MM/YYYY HH24:MI:SS')
AND codusur LIKE '32%';
```

### Por supervisores com contagem

```sql
SELECT DISTINCT A.CODUSUR, B.NOME, COUNT(*) AS QTD_PEDIDOS, A.CODSUPERVISOR AS SUPERVISOR
FROM MXSINTEGRACAOPEDIDO A
LEFT JOIN MXSUSUARIOS B ON A.CODUSUR = B.CODUSUR
WHERE A.DATA >= TO_DATE('01/05/2020', 'DD/MM/YYYY')
AND A.STATUS = 4
AND A.NUMPEDERP IS NOT NULL
GROUP BY A.CODUSUR, B.NOME, A.CODSUPERVISOR
ORDER BY TO_NUMBER(SUPERVISOR) ASC;
```

---

## 9. RCA ATIVOS/INATIVOS

### RCA ativos sem conexao nos ultimos 200 dias

```sql
SELECT status, codusuario, login, nome FROM mxsusuarios
WHERE tipousuario = 'R' AND status = 'A'
AND codusuario NOT IN
  (SELECT DISTINCT codusuario FROM mxsaparelhosconnlog
   WHERE TRUNC(dtterminoconexao) >= TRUNC(SYSDATE - 200));
```

### RCA ativos que nunca passaram pedidos

```sql
SELECT rp.CODUSUR, rp.LOGIN, rp.NOME, rp.CODUSUARIO, us.codsupervisor, us.telefone1
FROM mxsusuarios rp
INNER JOIN mxsusuari us ON rp.codusur = us.codusur
WHERE rp.tipousuario = 'R' AND rp.status = 'A'
AND rp.codusuario NOT IN (SELECT DISTINCT codusuario FROM mxsaparelhosconnlog)
AND us.dttermino IS NULL;
```

---

## 10. RELACAO DE LOGIN

```sql
SELECT LOGIN, CODUSUARIO, CODUSUR FROM mxsusuarios;
```

---

## 11. CONSULTAR FOTOS DE PRODUTOS

```sql
-- Foto de produto especifico
SELECT CODPROD, DIRFOTOPROD, DESCRICAO FROM pcprodut WHERE codprod = 6152;

-- Fotos atualizadas recentemente
SELECT * FROM MXSPRODUTOSFOTOS ORDER BY DTATUALIZ DESC;

-- Produtos com diretorio de foto
SELECT DIRFOTOPROD FROM MXSPRODUT WHERE DIRFOTOPROD IS NOT NULL;

-- Fotos sem diretorio mas com link
SELECT A.CODPROD, A.DIRFOTOPROD, B.HASH, B.LINK, B.DTATUALIZ, B.CODOPERACAO
FROM MXSPRODUT A INNER JOIN MXSPRODUTOSFOTOS B ON A.CODPROD = B.CODPROD
WHERE A.DIRFOTOPROD IS NULL;
```

### Converter extensao de fotos de BMP para JPG

```sql
-- Visualizar mudanca
SELECT dirfotoprod,
  REPLACE(REPLACE(dirfotoprod, '.bmp','.JPG'),'.BMP','.JPG') novodirfotoprod,
  codprod FROM pcprodut WHERE dirfotoprod IS NOT NULL;

-- Aplicar conversao
UPDATE pcprodut SET dirfotoprod = REPLACE(REPLACE(dirfotoprod, '.bmp','.JPG'),'.BMP','.JPG')
WHERE dirfotoprod IS NOT NULL;
```

---

## 12. ITENS DO PEDIDO PELO JSON

```sql
SELECT ID_PEDIDO, NUMPED, PJSON.CODPRODUTO, PJSON.DESCRICAO,
  PJSON.QUANTIDADE, PJSON.PRECOVENDA, PJSONCLI.CODIGO CODCLI
FROM MXSINTEGRACAOPEDIDO PED_TAB,
  JSON_TABLE(OBJETO_JSON, '$.Produtos[*]' COLUMNS (
    ROW_NUMBER FOR ORDINALITY,
    CODPRODUTO VARCHAR(10) PATH '$.Codigo',
    DESCRICAO VARCHAR(2000) PATH '$.Descricao',
    QUANTIDADE VARCHAR(10) PATH '$.Quantidade',
    PRECOVENDA VARCHAR(100) PATH '$.PrecoVenda'
  )) PJSON,
  JSON_TABLE(OBJETO_JSON, '$.Cliente[*]' COLUMNS (
    ROW_NUMBER FOR ORDINALITY,
    CODIGO VARCHAR(10) PATH '$.Codigo'
  )) PJSONCLI
WHERE PED_TAB.NUMPED = :numped AND PED_TAB.CODUSUARIO = :codusuario;
```

---

## 13. TITULOS ABERTOS COM STATUS 51

```sql
UPDATE ERP_MXSPREST SET CODOPERACAO = 2
WHERE TRANSLATE(UPPER(PREST),'0123456789,./-ABCDEFGHIJKLMNOPQRSTUVWXYZC','0123456789') IS NOT NULL
AND CAST(TRANSLATE(UPPER(PREST),'0123456789,./-ABCDEFGHIJKLMNOPQRSTUVWXYZC','0123456789') AS NUMBER) > 50
AND CODOPERACAO != 2;
```

---

## 14. DEIXAR APENAS TITULOS VENCIDOS NA APK

Parametro: `ENVIAR_APENAS_TITULOS_VENCIDOS = S`

```sql
-- Verificar titulos
SELECT CODOPERACAO, A.* FROM ERP_MXSPREST A
WHERE NUMTRANSVENDA IN (SELECT NUMTRANSVENDA FROM MXSTITULOSABERTOS WHERE VENCIDO = 'S');

SELECT * FROM mxstitulosabertos WHERE codoperacao != 2 AND DTVENC >= SYSDATE;

-- Remover titulos nao vencidos
BEGIN
  FOR DADOS IN (SELECT * FROM ERP_MXSPREST
    WHERE NUMTRANSVENDA IN (SELECT NUMTRANSVENDA FROM MXSTITULOSABERTOS WHERE VENCIDO = 'N')) LOOP
    UPDATE ERP_MXSPREST SET CODOPERACAO = 2 WHERE NUMTRANSVENDA = DADOS.NUMTRANSVENDA;
    UPDATE MXSTITULOSABERTOS SET CODOPERACAO = 2 WHERE VENCIDO = 'N' AND CODOPERACAO != 2;
    COMMIT;
  END LOOP;
END;
```

---

## 15. DATA DOS PEDIDOS

```sql
SELECT NUMPED, CODUSUR, CODUSUARIO, DTINCLUSAO AS INCLUSAONUVEM,
  DTENVIOERP, VLATEND, DTPROCESSAMENTOERP, DTGRAVACAOERP
FROM MXSINTEGRACAOPEDIDO
WHERE DTINCLUSAO > TO_DATE('31/07/2019', 'DD/MM/YYYY');

-- Filtros por data
WHERE TRUNC(data) = TRUNC(sysdate);
WHERE data BETWEEN '06-sep-2019' AND '06-sep-2019';
BETWEEN TRUNC(TO_DATE('01/08/2019','dd/MM/yyyy')) AND TRUNC(TO_DATE('17/10/2019','dd/MM/yyyy'));
```

---

## 16. USUARIOS COM VERSAO DIFERENTE

```sql
SELECT DISTINCT(A.CODUSUARIO) AS USUARIO_MAXIMA, A.APPVERSION AS VERSAO, U.NOME, U.CODUSUR
FROM MXSAPARELHOSCONNLOG A
INNER JOIN MXSUSUARIOS U ON A.CODUSUARIO = U.CODUSUARIO
WHERE TRUNC(A.DTATUALIZ) >= TRUNC(SYSDATE) AND APPVERSION != '(VERSAO)';
```

---

## 17. DESCONTO PROGRESSIVO (CAMPANHA COLGATE / P&G)

### Tabelas envolvidas - P&G

```sql
SELECT * FROM PCGRUPOCOMBOSKUC WHERE CODGRUPOCOMBOSKU IN (:codigo);
SELECT * FROM PCGRUPOCOMBOSKUCOMBO WHERE CODCOMBO IN (:codigos);
SELECT * FROM PCGRUPOCOMBOSKUFAIXA WHERE CODGRUPOCOMBOSKU = :codigo;
SELECT * FROM PCGRUPOCOMBOSKUFAIXADESC WHERE CODGRUPOCOMBOSKUFAIXA = :codigo;
SELECT * FROM MXSPEGFAIXA WHERE CODGRUPOCOMBOSKU = :codigo;
SELECT * FROM MXSPEGLAMINA WHERE CODPROD = :codprod;
SELECT * FROM MXSPEGPRODUTOS;
```

### Tabelas envolvidas - Campanha Colgate

```sql
SELECT * FROM MXSCAMPANHAFAMILIA WHERE CODIGO IN (:codigo);
SELECT * FROM MXSCAMPANHAFAIXAS WHERE CODIGOCAMPANHA IN (:codigo);
SELECT * FROM MXSCAMPANHAFAMILIAGRUPOS WHERE CODIGOCAMPANHA IN (:codigo);
SELECT * FROM MXSGRUPOSCAMPANHAC WHERE CODGRUPO IN (:codigo);
SELECT * FROM MXSFAMILIAITENS WHERE CODIGOFAMILIA IN (:codigo);
SELECT * FROM MXSFILTROCAMPANHA; -- restricoes: tiporestricao = 1 (restrito) / 2 (exclusivo)
SELECT * FROM MXSDESCPROGRESSIVOCLIENT WHERE CODCLI IN (:codcli);
```

### Regras do desconto progressivo

1. Parametro `TIPO_DESC_PROGRESSIVO` deve estar como `PRG` na MXSPARAMETRO (nao e possivel utilizar com desconto P&G)
2. Nao acumula flexivel (afeta estrutura de CC do cliente)
3. Utilizar novo portal (Central de Configuracao)
4. Cadastrar familia de produtos
5. Cadastrar informacoes da campanha:
   - Acumulativo/Pedido
   - Data de vigencia
   - % total do pedido e Qt maxima
   - Condicoes de familia (qt minima, valor minimo)
   - Condicoes da faixa (qt minima, valor minimo, desconto aplicado)
   - Restricoes Exclusivas (aplica APENAS para os selecionados)
   - Restricoes Restritas (bloqueados, NAO aplica para os selecionados)

---

## 18. LIMPAR REGISTRO PCMXSINTEGRACAO

```sql
BEGIN
  FOR DADOS IN (SELECT ID FROM PCMXSINTEGRACAO WHERE TABELA = 'MXSTABPRCLI' AND STATUS = -1) LOOP
    DELETE FROM PCMXSINTEGRACAO WHERE ID = DADOS.ID;
    COMMIT;
  END LOOP;
END;
```

---

## 19. CONSULTAR PEDIDO JSON (SCHEMA ESPECIFICO)

```sql
SELECT ID_PEDIDO, NUMPED, PJSON.CODPRODUTO, PJSON.DESCRICAO,
  PJSON.QUANTIDADE, PJSON.PRECOVENDA
FROM SCHEMA.MXSINTEGRACAOPEDIDO PED_TAB,
  JSON_TABLE(OBJETO_JSON, '$.Produtos[*]' COLUMNS (
    ROW_NUMBER FOR ORDINALITY,
    CODPRODUTO VARCHAR(10) PATH '$.Codigo',
    DESCRICAO VARCHAR(2000) PATH '$.Descricao',
    QUANTIDADE VARCHAR(10) PATH '$.Quantidade',
    PRECOVENDA VARCHAR(100) PATH '$.PrecoVenda'
  )) PJSON
WHERE PED_TAB.NUMPED = :numped AND PED_TAB.CODUSUARIO = :codusuario;
```

---

## 20. JOBS DO BANCO ORACLE

```sql
-- Verificar parametro de jobs
SELECT name, value FROM v$parameter WHERE name LIKE '%job_queue_processes%';

-- Listar jobs agendados
SELECT * FROM dba_scheduler_jobs;
```

---

## 21. PROCURAR TABELA E COLUNA NO ORACLE

```sql
SELECT * FROM cols
WHERE table_name LIKE '%NOME_TABELA%'
AND column_name LIKE '%NOME_COLUNA%';
```

---

## 22. SERVICE NAME DO ORACLE

```sql
SELECT VALUE FROM V$PARAMETER WHERE NAME = 'SERVICE_NAMES';
```

---

## 23. SESSOES BLOQUEADAS (LOCKED)

### Verificar SID atual

```sql
SELECT DISTINCT sid FROM v$mystat;
SELECT * FROM v$session WHERE sid = :sid;
```

### Sessoes bloqueando outras

```sql
SELECT sid, serial#, status, username, osuser, program, blocking_session blocking, event
FROM v$session WHERE blocking_session IS NOT NULL;
```

### Via DBA_WAITERS

```sql
SELECT waiting_session, holding_session FROM dba_waiters;
```

### Locks ativos

```sql
SELECT * FROM v$lock WHERE block <> 0;
```

### Consulta detalhada de locks (GV$)

```sql
SELECT DECODE(L.BLOCK, 0, 'Em espera', 'Bloqueando ->') USER_STATUS,
  CHR(39) || S.SID || ',' || S.SERIAL# || CHR(39) SID_SERIAL,
  S.SID, S.PROGRAM, S.SCHEMANAME, S.OSUSER, S.MACHINE,
  DECODE(L.TYPE, 'RT','Redo Log Buffer','TD','Dictionary',
    'TM','DML','TS','Temp Segments','TX','Transaction',
    'UL','User','RW','Row Wait',L.TYPE) LOCK_TYPE,
  DECODE(L.LMODE, 0,'None',1,'Null',2,'Row Share',3,'Row Excl.',
    4,'Share',5,'S/Row Excl.',6,'Exclusive', LTRIM(TO_CHAR(LMODE,'990'))) LOCK_MODE,
  CTIME, OBJECT_NAME
FROM GV$LOCK L
JOIN GV$SESSION S ON (L.INST_ID = S.INST_ID AND L.SID = S.SID)
JOIN GV$LOCKED_OBJECT O ON (O.INST_ID = S.INST_ID AND S.SID = O.SESSION_ID)
JOIN DBA_OBJECTS D ON (D.OBJECT_ID = O.OBJECT_ID)
WHERE (L.ID1, L.ID2, L.TYPE) IN (SELECT ID1, ID2, TYPE FROM GV$LOCK WHERE REQUEST > 0)
ORDER BY 13 DESC;
```

### Locks na PCMXSINTEGRACAO

```sql
SELECT DISTINCT SES.PROGRAM EXECUTAVEL, OBJ.OBJECT_NAME TABELA,
  SES.STATUS, SES.SID, SES.SERIAL#, SQL.SQL_TEXT TEXTO_SQL,
  SES.MACHINE MAQUINA, SES.USERNAME USUARIO_ORACLE, SES.OSUSER USUARIOS_SO
FROM V$SESSION SES, V$LOCKED_OBJECT LOC, DBA_OBJECTS OBJ, V$SQL SQL
WHERE SES.SID = LOC.SESSION_ID
AND LOC.OBJECT_ID = OBJ.OBJECT_ID
AND SES.SQL_ADDRESS = SQL.ADDRESS(+)
AND OBJ.OBJECT_NAME LIKE '%PCMXSINTEGRACAO%'
ORDER BY SES.LAST_CALL_ET DESC;
```

---

## 24. SESSAO ABERTA DE USUARIO ORACLE

```sql
-- Verificar sessoes
SELECT SID, SERIAL#, STATUS FROM v$session WHERE username = :usuario;

-- Matar sessao
ALTER SYSTEM KILL SESSION ':sid,:serial#' IMMEDIATE;

-- Gerar comandos para matar sessoes ativas
SELECT 'alter system kill session ''' || SID || ',' || SERIAL# || ''' immediate;'
FROM V$SESSION WHERE USERNAME = 'MAXSOLUCOES' AND STATUS = 'ACTIVE';
```

---

## 25. CRIAR USUARIO NO ORACLE

```sql
ALTER SESSION SET "_ORACLE_SCRIPT" = true;
CREATE USER nome_usuario IDENTIFIED BY senha_usuario DEFAULT TABLESPACE users;
GRANT connect, resource TO nome_usuario;
```

---

## 26. GRANTS MAXSOLUCOES

Script para conceder permissoes completas (SELECT, UPDATE, INSERT, DELETE, EXECUTE, DEBUG) e criar sinonimos entre schemas.

Tabelas envolvidas: MXSACESSODADOS, MXSACESSOENTIDADES, MXSACESSOROTINAS, MXSCONFIG, MXSCONFIGDATA, MXSDADOS, MXSMODULOS, MXSPARAMETRO, MXSPARAMETROVALOR, MXSPERFILACESSO, MXSPERFILDADOS, MXSPERFILENTIDADES, MXSPERFILROTINAS, MXSROTINAS, MXSROTINASI, MXSUSUARIOS.

---

## 27. VERIFICAR PRODUTOS DUPLICADOS

```sql
SELECT codauxiliar, codprod, COUNT(*) FROM mxsembalagem
GROUP BY codauxiliar, codprod HAVING COUNT(*) > 1;
```

---

## 28. VERIFICAR CLIENTES DUPLICADOS NA PCCLIENTFV

```sql
SELECT CLIENTE, COUNT(*) FROM PCCLIENTFV
WHERE CGCENT = ':cnpj'
AND TRUNC(DTINCLUSAO) = TRUNC(SYSDATE) GROUP BY CLIENTE;
```

### Apagar registros duplicados

```sql
BEGIN
  FOR DADOS IN (SELECT ROWID, CGCENT FROM PCCLIENTFV A
    WHERE ROWID IN (SELECT MIN(ROWID) FROM PCCLIENTFV B WHERE A.CGCENT = B.CGCENT)
    AND A.TIPOOPERACAO = 'A') LOOP
    DELETE FROM PCCLIENTFV WHERE ROWID != DADOS.ROWID AND CGCENT = DADOS.CGCENT;
    COMMIT;
  END LOOP;
END;
```

---

## 29. ENVIAR INFORMACOES COMPLETAS DE UMA TABELA AOS VENDEDORES

```sql
BEGIN
  FOR DADOS IN (SELECT CODUSUARIO FROM MXSUSUARIOS WHERE STATUS = 'A') LOOP
    INSERT INTO DELTA_ENVIOS (CODUSUARIO, TABELA) VALUES (DADOS.CODUSUARIO, 'MXSDESCONTO');
    COMMIT;
  END LOOP;
END;
```

---

## 30. MIX DE PRODUTOS VENDIDOS NO MES

```sql
SELECT DISTINCT CODPROD, COUNT(*)
FROM MXSHISTORICOPEDI WHERE NUMPED IN
  (SELECT NUMPED FROM MXSHISTORICOPEDC WHERE CODUSUR = :codusur AND POSICAO = 'F')
AND DATA BETWEEN TO_DATE(':dt_inicio','DD/MM/YYYY') AND TO_DATE(':dt_fim','DD/MM/YYYY')
GROUP BY CODPROD;
```

---

## 31. REGISTROS DO HISTORICO

### Contagem MXSHISTORICOPEDC

```sql
SELECT COUNT(*) AS MXSHISTORICOPEDC FROM MXSHISTORICOPEDC
WHERE POSICAO != 'C' AND CODFILIAL IN (1) AND CODOPERACAO != 2
AND DATA BETWEEN TRUNC(TO_DATE(':dt_inicio','dd/MM/yyyy'))
AND TRUNC(TO_DATE(':dt_fim','dd/MM/yyyy'));
```

### Contagem MXSHISTORICOPEDI

```sql
SELECT COUNT(*) AS MXSHISTORICOPEDI FROM MXSHISTORICOPEDI
INNER JOIN MXSHISTORICOPEDC ON MXSHISTORICOPEDC.NUMPED = MXSHISTORICOPEDI.NUMPED
WHERE MXSHISTORICOPEDC.POSICAO != 'C' AND MXSHISTORICOPEDI.POSICAO != 'C'
AND MXSHISTORICOPEDC.DATA BETWEEN TRUNC(TO_DATE(':dt_inicio','dd/MM/yyyy'))
AND TRUNC(TO_DATE(':dt_fim','dd/MM/yyyy'))
AND MXSHISTORICOPEDC.CODFILIAL IN ('1')
AND MXSHISTORICOPEDI.CODOPERACAO != 2 AND MXSHISTORICOPEDC.CODOPERACAO != 2;
```

---

## 32. NORMALIZAR INFORMACOES MXSMOV

```sql
SELECT COUNT(1) FROM ERP_MXSMOV
WHERE CODOPERACAO != 2 AND DTMOV >= TO_DATE(':dt_inicio','dd/MM/yyyy')
AND DTATUALIZ >= TO_DATE(':dt_atualizacao','dd/MM/yyyy hh24:mi:ss');
```

---

## 33. CONSULTAR PEDIDO E FILIAL

```sql
SELECT codfilial, codfilialnf, i.* FROM PCPEDC i WHERE numpedrca = :numpedrca;
```

---

## 34. PEDIDOS PENDENTES NO APARELHO

```sql
SELECT qtpedprev FROM mxsusuari WHERE codusur = :codusur;
-- Verificar parametro: BLK_CONN_CONSIDERAPEDBLOQCOMOPEND
```

---

## 35. CONSULTAR MARGEM DE LUCRATIVIDADE

```sql
SELECT * FROM pcparamfilial WHERE nome LIKE '%CON_MARGEMMIN%';
SELECT * FROM pcparamfilial WHERE nome LIKE '%BLOQPEDABAIXOMARGEMFV%';
-- OBS: verificar a permissao do usuario para bloquear os pedidos
```

---

## 36. COMPARAR MXSTABPR COM PCTABPR

```sql
SELECT mx.codprod, mx.numregiao,
  mx.pvenda AS pvendaMAXIMA, mx.pvendaatac1 AS vendaatacadoMAXIMA, mx.pvenda1 AS pvendaMAXIMA1,
  pc.pvenda AS pvendaPC, pc.pvenda1 AS pvendaPC1, pc.ptabelaatac AS pvatacadoPC
FROM mxstabpr mx
JOIN pctabpr pc ON mx.codprod = pc.codprod
WHERE mx.codprod = :codprod AND mx.numregiao = :numregiao;
```

---

## 37. CONSULTAR REGIAO E PRACA DO CLIENTE

```sql
SELECT C.CLIENTE, C.CODCLI, C.CODPRACA, N.NUMREGIAO, R.REGIAO
FROM MXSCLIENT C
LEFT JOIN MXSPRACA N ON C.CODPRACA = N.CODPRACA
LEFT JOIN MXSREGIAO R ON R.NUMREGIAO = N.NUMREGIAO
WHERE CODCLI IN (:codcli);
```

---

## 38. IMPOSTO DO PRODUTO

```sql
SELECT P.CODPROD, P.DESCRICAO, PP.CODFILIAL, PP.CALCULAIPI,
  TP.NUMREGIAO, TP.PVENDA, TP.VLST, TP.CODST, TP.PVENDASEMIMPOSTO1, TP.VLFCPST,
  TT.CODFILIALNF, TT.UFDESTINO, TT.CODST AS CODST_TABTRIB,
  TB.CODST AS CODST_TRIBUT, TB.IVA, TB.ALIQICMS1, TB.ALIQICMS2
FROM MXSPRODUT P
LEFT JOIN MXSPRODFILIAL PP ON P.CODPROD = PP.CODPROD
LEFT JOIN MXSTABPR TP ON P.CODPROD = TP.CODPROD
LEFT JOIN MXSTABTRIB TT ON P.CODPROD = TT.CODPROD
LEFT JOIN MXSTRIBUT TB ON TT.CODST = TB.CODST
WHERE P.CODPROD IN (:codprod)
AND PP.CODFILIAL IN (:codfilial) AND TT.CODFILIALNF IN (:codfilialnf)
AND TP.NUMREGIAO IN (:numregiao);
```

---

## 39. IMPOSTO PELO CLIENTE

```sql
SELECT C.CODCLI, C.CLIENTE, C.TIPOFJ, C.CONSUMIDORFINAL, C.CALCULAST,
  C.CLIENTEFONTEST, C.FORCACLIPJ, C.FORCECLIPF, C.ISENCAOSUFRAMA,
  C.CONTRIBUINTE, C.ISENTODIFALIQUOTAS, C.ISENTOICMS, C.ISENTOIPI,
  C.IVAFONTE, C.IEENT
FROM MXSCLIENT C WHERE C.CODCLI IN (:codcli);
```

---

## 40. CONSULTAR PLANO DE PAGAMENTO

```sql
-- Planos e cobrancas do cliente
SELECT * FROM PCPLPAGCLI WHERE codcli = :codcli ORDER BY codplpag;
SELECT * FROM PCCOBCLI WHERE codcli = :codcli;
SELECT codplpag, codcob, c.* FROM mxsclient c WHERE codcli = :codcli;
SELECT * FROM pcplpag WHERE codplpag = :codplpag;
SELECT * FROM pccob WHERE codcob LIKE '%CH%';
SELECT * FROM pccob WHERE nivelvenda >= 4;
```

### Validar "Nenhum plano de pagamento pode ser carregado para esse pedido"

```sql
-- 1. Validar plano e cobranca no cadastro do cliente (rotina 302)
SELECT a.CODCLI, a.CODPLPAG AS PLANO_CLI, b.ENVIAPLANOFV,
  a.CODCOB COB_CLI, c.ENVIACOBRANCAFV
FROM pcclient a
JOIN pcplpag b ON b.codplpag = a.codplpag
JOIN pccob c ON c.CODCOB = a.CODCOB
WHERE a.CODCLI = :codcli;

-- 2. Validar plano especial para o cliente
SELECT a.CODPLPAG AS PAG_ESPECIAL, b.ENVIAPLANOFV,
  c.CODCOB AS COB_VINCULADA, d.ENVIACOBRANCAFV
FROM pcplpagcli a
JOIN pcplpag b ON b.CODPLPAG = a.CODPLPAG
JOIN pccobplpag c ON c.CODPLPAG = a.CODPLPAG
JOIN pccob d ON d.CODCOB = c.CODCOB
WHERE codcli = :codcli;

-- 3. Verificar acesso do RCA ao plano no Portal ADMIN
SELECT b.LOGIN AS RCA, a.CHAVEDADOS AS PLANO_PAG, c.CHAVEDADOS AS COB
FROM mxsacessodados A
JOIN MXSUSUARIOS B ON B.CODUSUARIO = A.CODUSUARIO
JOIN mxsacessodados C ON C.CODUSUARIO = a.CODUSUARIO
WHERE A.CODUSUARIO = :codusuario AND A.coddados IN (1,2)
AND A.CHAVEDADOS IN (:codplpag) AND C.CHAVEDADOS IN (:codcob);

-- 4. Verificar SYNCs
SELECT * FROM sync_mxsplpag WHERE codplpag IN (:codplpag) ORDER BY dtatualiz DESC;
SELECT * FROM sync_mxscob WHERE codcob IN (:codcob) ORDER BY dtatualiz DESC;
SELECT * FROM sync_mxsplpagcli WHERE codcli IN (:codcli) ORDER BY dtatualiz DESC;
SELECT * FROM sync_mxscobplpag WHERE codplpag IN (:codplpag) ORDER BY dtatualiz DESC;
```

---

## 41. VERIFICAR METAS

```sql
SELECT * FROM MXSMETA WHERE CODUSUARIO = :codusuario AND TIPOMETA = 'F' ORDER BY 11 DESC;
SELECT * FROM PCMETA WHERE CODUSUR = :codusur AND TIPOMETA = 'F' ORDER BY DATA DESC;

-- Comparar metas entre PC e MXS
SELECT P.CODIGO AS CODIGOMETA, P.CODUSUR, P.TIPOMETA,
  P.VLVENDAPREV, P.QTVENDAPREV, P.MIXPREV,
  M.VLVENDAPREV AS VLPREVMAXIMA, M.QTVENDAPREV AS QTPREVMAXIMA, M.MIXPREV AS MIXPREVMAXIMA
FROM PCMETA P JOIN MXSMETA M ON P.CODIGO = M.CODIGO
WHERE P.CODUSUR = :codusur;

-- Resumo de metas do mes
SELECT CODUSUR, DATA,
  SUM(NVL(VLVENDAPREV, 0)) AS METAVENDA,
  SUM(NVL(MIXPREV, 0)) AS METAITENS,
  SUM(NVL(PEDIDOSPREV, 0)) AS METAPEDIDOS,
  SUM(NVL(CLIPOSPREV, 0)) AS METACLIENTES, CODFILIAL
FROM PCMETA
WHERE PCMETA.DATA BETWEEN TRUNC(SYSDATE,'mm') AND LAST_DAY(SYSDATE)
AND CODFILIAL IN (:codfilial) AND CODUSUR = :codusur AND TIPOMETA = 'M'
GROUP BY CODUSUR, DATA, CODFILIAL;
```

---

## 42. CONSULTAR CLIENTE PARA RCA

```sql
SELECT codcli, cliente, codusur1, codusur2 FROM pcclient WHERE codcli = :codcli;
```

---

## 43. VALIDAR COMPRAS

Se o criterio de venda for **P** (Pedido), consulta por pedidos na nuvem:

```sql
SELECT PC.CODCLI, PC.CODUSUR,
  TO_DATE('01/' || TO_CHAR(PC.DATA, 'MM/RRRR'), 'DD/MM/RRRR') AS DATA,
  SUM(PC.VLATEND) AS VALOR, MXSUSUARIOS.CODUSUARIO
FROM MXSUSUARI, MXSPLPAG, MXSHISTORICOPEDC PC, MXSUSUARIOS
WHERE PC.CODPLPAG = MXSPLPAG.CODPLPAG AND PC.CODOPERACAO != 2
AND PCRITERIOVENDA = 'P'
AND PC.CODUSUR = MXSUSUARI.CODUSUR AND PC.CODUSUR = MXSUSUARIOS.CODUSUR
AND PC.CONDVENDA NOT IN (4,8,10,13,20,98,99)
AND PC.DATA BETWEEN PDTINICIO AND PDTTERMINO
GROUP BY PC.CODCLI, MXSUSUARIOS.CODUSUARIO, PC.CODUSUR,
  TO_DATE('01/' || TO_CHAR(PC.DATA, 'MM/RRRR'), 'DD/MM/RRRR');
```

Se o criterio de venda for **F** (Faturado), filtra por `POSICAO = 'F'` tambem.

---

## 44. NORMALIZACAO AO ALTERAR PARAMETRO DE HISTORICO DE PEDIDOS

```sql
UPDATE MXSHISTORICOPEDC SET ATUALIZID = 0
WHERE ORIGEMPED = 'R' AND DATA >= TRUNC(SYSDATE) - 180;

UPDATE MXSHISTORICOPEDI SET ATUALIZID = 0
WHERE NUMPED IN (SELECT NUMPED FROM MXSHISTORICOPEDC
  WHERE ORIGEMPED = 'T' AND DATA >= TRUNC(SYSDATE) - 180);
```

---

## 45. CONSULTAR PARAMETRO 132

```sql
SELECT * FROM MXSPARAMFILIAL WHERE NOME LIKE '%FIL_BLOQUEARPEDIDOSABAIXOVLMINIMO%';
```

---

## 46. CONSULTAR LIMITE DE CREDITO

Query complexa que consulta limite de credito considerando: titulos abertos, pedidos pendentes, cheques, creditos e cliente principal.

Tabelas envolvidas: PCCLIENT, PCPREST, PCPEDC, PCCRECLI, PCPARAMFILIAL, PCCOB.

Parametro relevante: `SOMACREDITOCLIPRINCIPAL` (PCPARAMFILIAL, codfilial = 99).

---

## 47. CONSULTAR COMISSAO

```sql
-- Por regiao do produto
SELECT codprod, numregiao, percom FROM mxscomissaoregiao WHERE codprod = :codprod;

-- Comissao do vendedor NFSAID
SELECT SUM(VLTOTGER), SUM(COMISSAO) FROM pcnfsaid
WHERE codusur = :codusur AND dtsaida >= ':dt_inicio' AND dtsaida <= ':dt_fim';
```

---

## 48. CERCA ELETRONICA

Parametros relacionados:

```sql
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_EDGE_BLOCK%';
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_EDGE_METERS_SIZE%';
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_IS_REQUIRED_CONFEC_PEDIDO%';
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_TRACKING_ENABLED%';
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_TRACKING_INTERVAL%';
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_TRACKING_STARTTIME%';
SELECT * FROM mxsparametro WHERE nome LIKE '%GPS_TRACKING_STOPTIME%';
SELECT * FROM mxsparametro WHERE nome LIKE '%LIMITE_RAIO_CHECK_IN_OUT%';
```

---

## 49. CONSULTAR ORCAMENTO

```sql
SELECT * FROM pcORCAVENDAC WHERE NUMORCA = :numorca;
```

---

## 50. CONSULTAR VERSAO WINTHOR

```sql
SELECT * FROM PCVERSAOBD ORDER BY dtsincronizacao DESC;
```

---

## 51. ULTIMOS PRODUTOS VENDIDOS (12 MESES)

```sql
SELECT COUNT(DISTINCT codprod) FROM pcpedi
INNER JOIN pcpedc ON pcpedc.numped = pcpedi.numped
WHERE pcpedc.POSICAO = 'F' AND CODEMITENTE = 8888
AND pcpedc.DATA >= ADD_MONTHS(TRUNC(SYSDATE,'mm'), -12);
```

---

## 52. VERIFICAR VALIDADE DO PRODUTO

```sql
SELECT dtvenc, i.* FROM mxsprodut i WHERE codprod = :codprod;
SELECT DTVAL, I.* FROM pcestendereco I
WHERE codprod = :codprod
AND codendereco IN (SELECT codendereco FROM pcendereco WHERE tipoender = 'AP');
```

---

## 53. VERIFICAR HISTORICO DE PEDIDOS DO RCA COM CLIENTE

```sql
SELECT * FROM TABLE(sync.fn_mxshistoricopedc_pl(:codusuario)) WHERE codcli = :codcli;
```

---

## 54. VERIFICAR SE O CLIENTE ESTA BLOQUEADO

```sql
SELECT CODCLI, BLOQUEIO, dtmxsalter, dtexclusao, DTBLOQ, BLOQUEIODEFINITIVO,
  DTDESBLOQUEIO, DTULTALTER1203, MOTIVOEXCLUSAO, BLOQUEIOSEFAZ, BLOQUEIOSEFAZPED
FROM pcclient WHERE codcli = :codcli;
```

---

## 55. REGIAO DO CLIENTE

```sql
SELECT * FROM PCREGIAO;

SELECT CODUSUR1, CODUSUR2, CODUSUR3, BLOQUEIO, DTBLOQ, BLOQUEIODEFINITIVO,
  DTDESBLOQUEIO, DTULTALTER1203, MOTIVOEXCLUSAO, NUMREGIAOCLI, DTEXCLUSAO,
  CODPROFISSIONAL, CODATV1, BLOQUEIOSEFAZ, BLOQUEIOSEFAZPED, DTVENCLIMCRED,
  VALIDARCAMPANHABRINDE, P.*
FROM PCCLIENT P WHERE CODCLI IN (:codcli);
```

---

## 56. CONSULTAR CESTA

```sql
SELECT * FROM PCFORMPROD;
SELECT * FROM PCPRECOCESTAC;
SELECT * FROM PCPRECOCESTAI;
-- Tabelas: PCPRECOCESTAC / PCPRECOCESTAI / PCFORMPROD
```

---

## 57. PRODUTO NAO APARECE

Checklist de verificacao:

```sql
-- 1. Produto principal
SELECT CODPROD, DTEXCLUSAO, REVENDA, ENVIARFORCAVENDAS, CODFORNEC, CODEPTO, CODSEC,
  CODCATEGORIA, CODSUBCATEGORIA, OBS, OBS2 FROM MXSPRODUT WHERE CODPROD IN (:codprod);

-- 2. Produto por filial
SELECT CODPROD, ENVIARFORCAVENDAS, CODFILIAL, PROIBIDAVENDA FROM MXSPRODFILIAL WHERE CODPROD IN (:codprod);

-- 3. Embalagem
SELECT CODPROD, CODFILIAL, CODAUXILIAR, EMBALAGEM, QTUNIT FROM MXSEMBALAGEM WHERE CODPROD IN (:codprod);

-- 4. Fornecedor
SELECT CODFORNEC, REVENDA FROM MXSFORNEC WHERE CODFORNEC IN (:codfornec);

-- 5. Departamento e secao
SELECT * FROM MXSDEPTO WHERE CODEPTO IN (:codepto);
SELECT * FROM MXSSECAO WHERE CODSEC IN (:codsec);

-- 6. Tabela de precos
SELECT * FROM MXSTABPR WHERE CODPROD IN (:codprod);

-- 7. Estoque
SELECT * FROM MXSEST WHERE CODPROD IN (:codprod);

-- 8. Preco por cliente
SELECT * FROM MXSTABPRCLI WHERE codcli IN (:codcli);

-- 9. Restricoes de venda
SELECT * FROM MXSRESTRICAOVENDA WHERE CODUSUR IN (:codusur);
SELECT * FROM MXSUSURFORNEC WHERE CODUSUR IN (:codusur) AND CODFORNEC IN (:codfornec);
SELECT * FROM MXSUSURDEPSEC WHERE CODPROD IN (:codprod);
```

### Forcar reenvio do produto

```sql
UPDATE MXSPRODUT SET ATUALIZID = 0 WHERE CODOPERACAO != 2 AND CODPROD IN (:codprod); COMMIT;
UPDATE MXSEST SET ATUALIZID = 0 WHERE CODOPERACAO != 2 AND CODPROD IN (:codprod); COMMIT;
UPDATE MXSPRODFILIAL SET ATUALIZID = 0 WHERE CODOPERACAO != 2 AND CODPROD IN (:codprod); COMMIT;
UPDATE MXSEMBALAGEM SET ATUALIZID = 0 WHERE CODOPERACAO != 2 AND CODPROD IN (:codprod); COMMIT;
UPDATE MXSTABPR SET ATUALIZID = 0 WHERE CODOPERACAO != 2 AND CODPROD IN (:codprod); COMMIT;
```

### Validacao no ERP (PCPRODUT)

```sql
SELECT OBS, OBS2, REVENDA, ENVIARFORCAVENDAS, DTEXCLUSAO, P.* FROM PCPRODUT P WHERE CODPROD IN (:codprod);
SELECT * FROM PCEST WHERE CODPROD IN (:codprod);
SELECT CODFORNEC, CODSEC, CODEPTO, P.* FROM PCPRODUT P WHERE CODPROD IN (:codprod);
SELECT REVENDA, F.* FROM PCFORNEC F WHERE CODFORNEC IN (:codfornec);
SELECT * FROM PCDEPTO WHERE CODEPTO IN (:codepto);
SELECT * FROM PCSECAO WHERE CODSEC IN (:codsec);
SELECT * FROM PCEMBALAGEM WHERE CODPROD IN (:codprod);
SELECT FORALINHA, PF.* FROM PCPRODFILIAL PF WHERE CODPROD IN (:codprod);
SELECT * FROM PCRESTRICAOVENDA WHERE CODUSUR IN (:codusur);
SELECT * FROM PCUSURFORNEC WHERE CODUSUR IN (:codusur) AND CODFORNEC IN (:codfornec);
SELECT * FROM PCUSURDEPSEC WHERE CODPROD IN (:codprod);
```

---

## 58. AUTORIZACAO DE PRECO

```sql
UPDATE mxsautoripedidoc SET status = 3;
```

Status na MXSINTEGRACAOPEDIDO:
- 8 = Pendente autorizacao
- 9 = Autorizado
- 10 = Negado

---

## 59. VERIFICAR ROTEIRO

```sql
SELECT * FROM pcrotacli
WHERE dtproxvisita BETWEEN TO_DATE(':dt_inicio', 'DD/MM/YYYY') AND TO_DATE(':dt_fim', 'DD/MM/YYYY')
AND codusur = :codusur AND diasemana = ':dia';

SELECT codusuario, codcli, dtinicio, dttermino FROM mxscompromissos
WHERE codusuario = :codusuario AND codcli = :codcli;

SELECT * FROM PCROTACLI WHERE CODUSUR IN (:codusur) AND DTPROXVISITA = ':data' ORDER BY SEQUENCIA;
```

OBS: Se nao aparecer as informacoes, executar TRUNCATE na tabela MXSCOMPROMISSOS e rodar a job.

---

## 60. CAMPANHA NAO APARECE AO RCA

Verificar na rotina 3306 (CAMPANHA DE DESCONTO):

```sql
SELECT * FROM mxsdescontoc WHERE codigo = :codigo;
SELECT * FROM mxsdescontoi WHERE codigo = :codigo;
SELECT * FROM sync_mxsdescontoi WHERE codigo = :codigo;
SELECT * FROM sync_mxsdescontoc WHERE codigo = :codigo;
```

---

## 61. NUMERO PEDIDO RCA

```sql
SELECT MAX(NUMPED), MAX(NUMPEDRCA) FROM PCPEDC WHERE CODUSUR = :codusur;
SELECT PROXNUMPED, PROXNUMPEDFORCA FROM PCUSUARI WHERE CODUSUR = :codusur;
```

Regra de formacao:
- NUMPED = CODIGO_RCA x 1.000.000
- NUMPEDRCA FORCA = (CODIGO_RCA x 1.000.000) + 200.000

---

## 62. PEDIDOS NA INTEGRADORA

### Status de importacao (PCPEDCFV)

| Status | Descricao |
|--------|-----------|
| 1 | Pendente |
| 2 | Sucesso |
| 3 | Erro |
| 4 | Em processamento (temporario) |

### Origem do pedido (PCPEDCFV.ORIGEMPED)

| Codigo | Descricao |
|--------|-----------|
| T | Telemarketing |
| R | Balcao Reserva |
| B | Balcao |
| F | Forca de Vendas |
| C | Call Center |
| K | Broker |

```sql
-- Resumo de pedidos do dia
SELECT importado, COUNT(1) FROM pcpedcfv
WHERE dtinclusao >= TRUNC(sysdate) AND idpedidonv IS NOT NULL GROUP BY importado;

-- Detalhes de pedido especifico
SELECT IMPORTADO, IDPEDIDONV, NUMPEDRCA, ENVIADONV FROM PCPEDCFV WHERE IDPEDIDONV = :id;

-- Processar pedido manualmente
BEGIN
  integradora.importarpedido(1);
END;
```

---

## 63. INDENIZACAO

```sql
SELECT * FROM PCINDCFV WHERE NUMPEDRCA = :numpedrca;
SELECT * FROM PCINDIFV WHERE CODINDENIZ = :codindeniz;
SELECT * FROM PCINDIFV WHERE TRUNC(DTINCLUSAO) = TRUNC(SYSDATE) ORDER BY DTINCLUSAO DESC;
```

---

## 64. PROCESSAMENTO DO CADASTRO DE CLIENTE

```sql
SELECT PC.IMPORTADO,
  (CASE
    WHEN PC.IMPORTADO = 1 THEN 'ACABOU DE CHEGAR O CADASTRO'
    WHEN PC.IMPORTADO = 2 THEN 'PROCESSADO COM SUCESSO'
    WHEN PC.IMPORTADO = 3 THEN 'PROCESSADO COM FALHAS'
    ELSE 'EM PROCESSAMENTO PELA INTEGRADORA'
  END) AS POSICAO_DO_CADASTRO,
  COUNT(*) AS QTD_REGISTROS
FROM PCCLIENTFV PC
WHERE TRUNC(DTINCLUSAO) = TRUNC(SYSDATE)
GROUP BY PC.IMPORTADO,
  (CASE
    WHEN PC.IMPORTADO = 1 THEN 'ACABOU DE CHEGAR O CADASTRO'
    WHEN PC.IMPORTADO = 2 THEN 'PROCESSADO COM SUCESSO'
    WHEN PC.IMPORTADO = 3 THEN 'PROCESSADO COM FALHAS'
    ELSE 'EM PROCESSAMENTO PELA INTEGRADORA'
  END)
ORDER BY PC.IMPORTADO;
```

---

## 65. RECRIAR JOB

### Passo a passo

1. Entrar no banco com usuario SYSTEM (ou SYS, MXMAUSR)
2. Executar: `ALTER SYSTEM SET job_queue_processes = 0;`
3. Aguardar 1 minuto
4. Executar: `ALTER SYSTEM SET job_queue_processes = 30;`
5. Recriar todas as JOBs (selecionar > create script > marcar editor > confirmar > F5)
6. Verificar se estao executando automaticamente

### Verificar jobs

```sql
SELECT name, value FROM v$parameter WHERE name LIKE '%job_queue_processes%';
SELECT owner, object_name, last_ddl_time, status FROM all_objects
WHERE object_name LIKE '%JOB%' AND object_type = 'JOB' AND OWNER LIKE '%MXSPEDIDOVENDA';
```

---

## 66. UPDATE PARA MAIS DE UMA TABELA

```sql
SELECT 'UPDATE ' || TABLE_NAME || ' SET CODOPERACAO = 2;' FROM USER_TABLES;
```

---

## 67. CRIAR TABLESPACE

```sql
-- Descobrir caminho do tablespace
SELECT ddf.tablespace_name "TablespaceName", ddf.file_name "DataFile",
  ddf.bytes/(1024*1024) "Total(MB)",
  ROUND((ddf.bytes - SUM(NVL(dfs.bytes,0)))/(1024*1024),1) "Used(MB)",
  ROUND(SUM(NVL(dfs.bytes,0))/(1024*1024),1) "Free(MB)"
FROM sys.dba_free_space dfs LEFT JOIN sys.dba_data_files ddf ON dfs.file_id = ddf.file_id
GROUP BY ddf.tablespace_name, ddf.file_name, ddf.bytes
ORDER BY ddf.tablespace_name, ddf.file_name;

-- Criar tablespace
CREATE TABLESPACE TS_MAXSOLUCOES DATAFILE '/u01/oradata/WINT/maxima/TS_MAXSOLUCOES.DBF'
SIZE 2G REUSE AUTOEXTEND ON MAXSIZE UNLIMITED;
```

---

## 68. TV7 - COMPORTAMENTO INTEGRADORA

Regras:
- Integradora processa TV7 somente com parametro 2542 = Sim (Reserva Estoque TV7)
- TV8 com parametro 2542 = Sim ou TV7/TV8 com parametro = Nao sao barrados
- No processamento TV7 com parametro = Sim, gera automaticamente pedido TV8 com estoque reservado
- Expedicao pela rotina 1459 (Expedicao Venda Assistida)
- Faturamento pelas rotinas 1402 ou 1432
- Se enviar TV8 com parametro = Sim, Integradora converte em TV1

---

## 69. AJUSTAR PROBLEMA DE ROTA DNS LINUX

1. Ajustar `/etc/hosts`
2. Editar `/etc/resolv.conf` com nameservers 8.8.8.8 e 8.8.4.4
3. Instalar resolvconf: `sudo apt install resolvconf`
4. Iniciar servico: `sudo systemctl start resolvconf.service && sudo systemctl enable resolvconf.service`
5. Editar `/etc/resolvconf/resolv.conf.d/head`
6. Reiniciar maquina: `reboot`
7. Travar arquivo: `chattr +i /etc/resolv.conf`

---

## 70. TIMEZONE LINUX

```bash
# Instalar TZ-Data no Alpine
apk add tzdata
# ENV = TZ = TIMEZONE (ex: America/Sao_Paulo)
```

---

## 71. MIGRACAO RANCHER PARA PORTAINER

### Passo a passo

1. Limpar cache do portainer antigo:
```bash
cd /var/lib/docker/volumes/portainer_data/_data
rm -rf *
```

2. Preparar ambiente:
```bash
cd /
wget -q https://maxsolucoes-versoes.s3.amazonaws.com/extrator/v2/install/ambiente.sh
chmod 777 ambiente.sh && sh ambiente.sh
```

3. Instalar novo Portainer (porta 9000):
```bash
docker run -d -p 19851:9000 --name MXS_Portainer --restart always \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data portainer/portainer \
  --admin-password '[DEFINIR_SENHA_SEGURA]' \
  --logo "https://maxsolucoes-versoes.s3.amazonaws.com/extrator/v1/logo/logo-maxima.png"
```

---

## 72. COMPOSE DO EXTRATOR NO PORTAINER

```yaml
version: '2'
services:
  MXS-Extrator_Nome_Cliente:
    privileged: true
    image: dockermaxima/extrator:3.1.2.42
    environment:
      USUARIO_EXTRATOR_NUVEM: [DEFINIR]
      SENHA_EXTRATOR_NUVEM: [DEFINIR_CRIPTOGRAFADO]
      USUARIO_SYSTEM_WINTHOR: [DEFINIR]
      SENHA_SYSTEM_WINTHOR: [DEFINIR_CRIPTOGRAFADO]
      DIRETORIO_FOTOS_WINTHOR: [DEFINIR]
    container_name: MXS-Extrator_Nome_Cliente
    restart: on-failure
    network_mode: bridge
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /app/maxima/MXS_Extrator/imagens_data:/mnt/maxima/produtos_fotos
      - /app/maxima/MXS_Extrator/extrator_prd_data/Conf:/app/maxima_extrator/extrator_prd/Conf
      - /app/maxima/MXS_Extrator/extrator_prd_data/LOGS:/app/maxima_extrator/extrator_prd/LOGS
      - /app/maxima/MXS_Extrator/extrator_prd_data/id:/app/maxima
    tty: true
    ports:
      - 9002:81/tcp
```

### Testar conexao com banco

```bash
nc -vz 192.168.154.7 1521
```

---

## 73. RELATORIO DA ROTINA 800

### Parametros necessarios

Inserir na MXSPARAMETRO:
- `URL_RELATORIO_800` - URL do webservice
- `PARAMETROS_CODUSUR_REL_800` - Variavel do relatorio
- `TOKEN_RELATORIO_800` - Token JWT (solicitar ao responsavel)

```sql
-- Inserir parametro URL
INSERT INTO MXSPARAMETRO (CODPARAMETRO, TITULO, NOME, VALOR, TIPODADO, TIPOPARAMETRO, OCULTO, ATUALIZID, DTATUALIZ, CODOPERACAO)
VALUES (MXSPARAMETRO_SEQ.NEXTVAL, 'RELATORIO800 URL', 'URL_RELATORIO_800', NULL, 1, 'G', 'S', '-9999999999', SYSDATE, 0);
COMMIT;

-- Inserir parametro CODUSUR
INSERT INTO MXSPARAMETRO (CODPARAMETRO, TITULO, NOME, VALOR, TIPODADO, TIPOPARAMETRO, OCULTO, ATUALIZID, DTATUALIZ, CODOPERACAO)
VALUES (MXSPARAMETRO_SEQ.NEXTVAL, 'RELATORIO800 PARAMETRO', 'PARAMETROS_CODUSUR_REL_800', 1, NULL, 'G', 'S', '-9999999999', SYSDATE, 0);
COMMIT;

-- Inserir parametro TOKEN (obter token valido com o responsavel)
INSERT INTO MXSPARAMETRO (CODPARAMETRO, TITULO, NOME, VALOR, TIPODADO, TIPOPARAMETRO, OCULTO, ATUALIZID, DTATUALIZ, CODOPERACAO)
VALUES (MXSPARAMETRO_SEQ.NEXTVAL, 'RELATORIO800 TOKEN', 'TOKEN_RELATORIO_800', '[TOKEN_JWT]', 1, 'G', 'S', '-9999999999', SYSDATE, 0);
COMMIT;

-- Atribuir valores
UPDATE MXSPARAMETRO SET VALOR = '[URL]' WHERE nome = 'URL_RELATORIO_800';
UPDATE MXSPARAMETRO SET VALOR = '[CODUSUR]' WHERE nome = 'PARAMETROS_CODUSUR_REL_800';
COMMIT;
```

---

## 74. RELATORIO DE PRODUTO COM CLASSIFICACAO DE PRECO

```sql
SELECT A.CODPROD AS PRODUTO, B.DESCRICAO, A.PVENDA AS PRECO,
  (CASE
    WHEN A.PVENDA <= 8 THEN 'PRODUTO BARATO'
    WHEN A.PVENDA >= 9 AND A.PVENDA <= 15 THEN 'PRODUTO EM CONTA'
    ELSE 'PRODUTO CARO'
  END) AS STATUS_PRECO
FROM MXSTABPR A INNER JOIN MXSPRODUT B ON A.CODPROD = B.CODPROD
GROUP BY A.CODPROD, B.DESCRICAO, A.PVENDA,
  (CASE
    WHEN A.PVENDA <= 25 THEN 'PRODUTO BARATO'
    WHEN A.PVENDA >= 26 AND A.PVENDA <= 60 THEN 'PRODUTO EM CONTA'
    ELSE 'PRODUTO CARO'
  END)
HAVING (A.PVENDA) > 1 ORDER BY A.PVENDA;
```

---

## 75. CONSULTAS SQL DE SUPORTE GERAL

Consultas consolidadas do documento de conhecimentos gerais para manter SQL neste arquivo canônico.

### Consultar versao do banco de dados na nuvem

```sql
SELECT * FROM MXSVERSAOBD ORDER BY DTATUALIZACAO DESC;
```

### Consultar produtos positivados no mes para um vendedor

```sql
SELECT COUNT(DISTINCT CODPROD)
FROM MXSPRODUTPOS
WHERE TRUNC(DTPOSITIVACAO) BETWEEN TO_DATE('01/04/2024', 'DD/MM/YYYY')
                               AND TO_DATE('30/04/2024', 'DD/MM/YYYY')
  AND CODUSUARIO IN (SELECT CODUSUARIO FROM MXSUSUARIOS WHERE CODUSUR = 73);
```

### Verificar estoque disponivel e cotas de produto

```sql
-- Estoque disponivel (considerando bloqueado, pendente, reservado)
SELECT (QTESTGER - QTBLOQUEADA - QTPENDENTE - QTRESERV) AS ESTOQUEDISP, MXSEST.*
FROM MXSEST
WHERE CODPROD IN (7072);

-- Verificar cotas de produto por cliente
SELECT * FROM MXSPRODUSUR
WHERE CODCLI IN (19483) AND CODPROD IN (7072, 7081);
```

### Verificar se o servico de agendamento do Oracle esta rodando

```sql
SELECT sid, serial#, username, program, module
FROM v$session
WHERE program LIKE 'CJQ%' OR program LIKE 'J0%';
```

### Consultar devolucoes no resumo de vendas

```sql
SELECT CODUSUR, DTENT DATA,
       SUM(VLDEVOLUCAO
           - DECODE(OBTER_PARAMETRO('REDUZIRST_DADOS_RELATORIO', NULL, 'N'), 'S', NVL(VLST,0), 0)
           - DECODE(OBTER_PARAMETRO('REDUZIRIPI_DADOS_RELATORIO', NULL, 'N'), 'S', NVL(VLIPI,0), 0)) AS VLDEVOL
FROM (
  SELECT DISTINCT
         ERP_MXSNFENT.DTENT,
         ERP_MXSNFENT.CODUSURDEVOL CODUSUR,
         ROUND(NVL(ERP_MXSMOV.QT,0) *
               (NVL(ERP_MXSMOV.PUNIT,0) + NVL(ERP_MXSMOV.VLFREITE,0) +
                NVL(ERP_MXSMOV.VLOUTRASDESP,0) + NVL(ERP_MXSMOV.VLFREITE_RATEIO,0) +
                NVL(ERP_MXSMOV.VLOUTROS,0) - NVL(ERP_MXSMOV.VLREPASSE,0)), 2) VLDEVOLUCAO,
         ROUND(NVL(ERP_MXSMOV.QT,0) * NVL(ERP_MXSMOV.ST,0), 2) VLST,
         ROUND(NVL(ERP_MXSMOV.QT,0) * NVL(ERP_MXSMOV.VLIPI,0), 2) VLIPI
  FROM ERP_MXSNFENT
  INNER JOIN ERP_MXSESTCOM ON ERP_MXSNFENT.NUMTRANSENT = ERP_MXSESTCOM.NUMTRANSENT
  INNER JOIN ERP_MXSMOV ON ERP_MXSESTCOM.NUMTRANSENT = ERP_MXSMOV.NUMTRANSENT
  INNER JOIN MXSPRODUT ON MXSPRODUT.CODPROD = ERP_MXSMOV.CODPROD
  WHERE ERP_MXSNFENT.CODOPERACAO != 2
    AND ERP_MXSMOV.CODOPERACAO != 2
    AND ERP_MXSNFENT.DTENT BETWEEN TRUNC(TO_DATE('01/06/2025', 'dd/MM/yyyy'))
                               AND TRUNC(TO_DATE('30/06/2025', 'dd/MM/yyyy'))
    AND ERP_MXSNFENT.CODUSURDEVOL = 334
    AND ERP_MXSNFENT.CODFILIAL IN (3)
)
GROUP BY CODUSUR, DTENT;
```

### Validar integridade de produtos em tabelas relacionadas

```sql
SELECT P.CODPROD,
       CASE WHEN P.CODFORNEC IS NULL THEN 'P.CODFORNEC AUSENTE'
            WHEN F.CODFORNEC IS NULL THEN 'P.CODFORNEC INEXISTENTE EM MXSFORNEC'
            ELSE NULL END AS STATUS_CODFORNEC,
       CASE WHEN P.CODSEC IS NULL THEN 'P.CODSEC AUSENTE'
            WHEN S.CODSEC IS NULL THEN 'P.CODSEC INEXISTENTE EM MXSSECAO'
            ELSE NULL END AS STATUS_CODSEC,
       CASE WHEN P.CODEPTO IS NULL THEN 'P.CODEPTO AUSENTE'
            WHEN D.CODEPTO IS NULL THEN 'P.CODEPTO INEXISTENTE EM MXSDEPTO'
            ELSE NULL END AS STATUS_CODEPTO,
       CASE WHEN P.DTEXCLUSAO IS NOT NULL THEN 'P.DTEXCLUSAO PREENCHIDA' ELSE NULL END AS STATUS_P_DTEXCLUSAO,
       CASE WHEN P.ENVIARFORCAVENDAS <> 'S' THEN 'P.ENVIARFORCAVENDAS != S' ELSE NULL END AS STATUS_P_ENVIARFORCAVENDAS,
       CASE WHEN P.REVENDA <> 'S' THEN 'P.REVENDA != S' ELSE NULL END AS STATUS_P_REVENDA,
       CASE WHEN P.CODOPERACAO = 2 THEN 'P.CODOPERACAO = 2' ELSE NULL END AS STATUS_P_CODOPERACAO,
       CASE WHEN F.CODFORNEC IS NOT NULL AND F.CODOPERACAO = 2 THEN 'MXSFORNEC.CODOPERACAO = 2' ELSE NULL END AS STATUS_F_CODOPERACAO,
       CASE WHEN F.CODFORNEC IS NOT NULL AND F.REVENDA <> 'S' THEN 'MXSFORNEC.REVENDA != S' ELSE NULL END AS STATUS_F_REVENDA,
       CASE WHEN S.CODSEC IS NOT NULL AND S.CODOPERACAO = 2 THEN 'MXSSECAO.CODOPERACAO = 2' ELSE NULL END AS STATUS_S_CODOPERACAO,
       CASE WHEN D.CODEPTO IS NOT NULL AND D.CODOPERACAO = 2 THEN 'MXSDEPTO.CODOPERACAO = 2' ELSE NULL END AS STATUS_D_CODOPERACAO,
       CASE WHEN (P.CODFORNEC IS NULL OR F.CODFORNEC IS NULL OR
                  P.CODSEC IS NULL OR S.CODSEC IS NULL OR
                  P.CODEPTO IS NULL OR D.CODEPTO IS NULL OR
                  P.DTEXCLUSAO IS NOT NULL OR
                  P.ENVIARFORCAVENDAS <> 'S' OR
                  P.REVENDA <> 'S' OR
                  P.CODOPERACAO = 2 OR
                  F.CODOPERACAO = 2 OR
                  S.CODOPERACAO = 2 OR
                  D.CODOPERACAO = 2 OR
                  F.REVENDA <> 'S')
             THEN 'PENDENCIA ENCONTRADA'
             ELSE 'OK' END AS STATUS_GERAL
FROM MXSPRODUT P
LEFT JOIN MXSFORNEC F ON P.CODFORNEC = F.CODFORNEC
LEFT JOIN MXSSECAO S ON P.CODSEC = S.CODSEC
LEFT JOIN MXSDEPTO D ON P.CODEPTO = D.CODEPTO
WHERE P.CODPROD IN (284445);
```
