# Guia legado sintético

## Consulta

Se FLAG_X=1, não execute `--force`.
Exceto em homologação, confirme o código PED-123.
- Mantenha o campo NUMPED na consulta.

| Campo | Valor |
| --- | --- |
| FLAG_X | ativo |

[Manual](https://example.invalid/manual)
![Fluxo](figure.png)

```sql
SELECT NUMPED FROM PCPEDC WHERE FLAG_X = 1;
```
