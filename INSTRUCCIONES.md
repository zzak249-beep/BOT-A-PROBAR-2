# Comparador de cartera — cómo ponerlo a funcionar en Railway

## Los 3 archivos que necesitas (todos van en la MISMA carpeta)
- `combine_portfolio.py` — el script
- `requirements.txt` — le dice a Railway qué instalar (pandas, matplotlib)
- `ejemplo_formato.csv` — una plantilla con datos FICTICIOS, solo para comparar
  que tu export de verdad tiene las mismas columnas. No la uses para nada más.

## El error más probable si "no funciona"
Si creaste un **servicio nuevo en Railway** con estos archivos y le diste a
desplegar, Railway espera que el programa se quede corriendo (como tu bot).
Este script hace su trabajo y TERMINA — Railway lo verá como si se hubiera
"caído" y lo reiniciará en bucle, o el deploy quedará en rojo. Eso es normal
y no significa que el script esté roto.

**La solución es no desplegarlo como servicio.** Este script se ejecuta a
demanda dentro de un servicio que YA tienes corriendo (el de tu bot), no
como su propio servicio.

## Pasos exactos

1. Copia los 3 archivos de arriba dentro del repositorio de GitHub que ya
   usas para desplegar tu bot en Railway. Por ejemplo, en una carpeta nueva
   `portfolio/` dentro de ese repo.

2. Si ya tienes un `requirements.txt` en ese repo, NO lo sustituyas: abre el
   tuyo y añade estas dos líneas al final:
   ```
   pandas>=2.0
   matplotlib>=3.7
   ```
   Si no tienes ninguno, sube el `requirements.txt` de aquí tal cual, en la
   raíz del repo.

3. Haz commit y push:
   ```
   git add portfolio/ requirements.txt
   git commit -m "añade comparador de cartera"
   git push
   ```

4. Espera a que Railway termine de redesplegar tu servicio (lo ves en el
   dashboard). Con el `requirements.txt` actualizado, ya instalará pandas y
   matplotlib solo.

5. Desde tu ordenador, con el CLI de Railway instalado (`npm i -g @railway/cli`,
   luego `railway login` y `railway link` para conectarlo a tu proyecto):
   ```
   railway ssh -- python portfolio/combine_portfolio.py portfolio/ejemplo_formato.csv portfolio/ejemplo_formato.csv --out /tmp/prueba
   ```
   Si esto te da un resultado (aunque sea con el archivo de ejemplo repetido
   dos veces), TODO está bien instalado y conectado. El problema, si lo
   había, era solo de despliegue — no del script.

6. Sube tus CSV reales (los que exportas de TradingView) a esa misma carpeta
   del repo, haz commit y push otra vez, y corre:
   ```
   railway ssh -- python portfolio/combine_portfolio.py portfolio/DEF.csv portfolio/TREND.csv portfolio/REBOTE.csv --out portfolio/informe
   ```

7. Para traerte el PNG y los CSV de resultado a tu ordenador: como Railway
   SSH no soporta copiar archivos, la forma más simple es que el propio
   script, al final, imprima las rutas (ya lo hace) y que hagas otro
   `git add portfolio/informe* && git commit && git push` para que te
   queden guardados en el repo y los veas en GitHub directamente (el PNG se
   puede previsualizar en la web de GitHub sin descargar nada).

## Si el paso 5 falla, dime EXACTAMENTE qué mensaje de error da
"No funciona" sin el mensaje no me deja saber si es:
- `railway: command not found` → el CLI no se instaló bien
- `Project Token not found` / pide login → falta `railway login` o `railway link`
- `python: command not found` → prueba con `python3` en vez de `python`
- `ModuleNotFoundError: pandas` → el `requirements.txt` no se aplicó; revisa
  que esté en la RAÍZ del repo y que el redeploy haya terminado en verde
- Un error del propio script sobre columnas → compara tu CSV real con
  `ejemplo_formato.csv`: deben tener cabeceras parecidas (español o inglés)
