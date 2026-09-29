# URBANIKA — Entrevista Inicial de Mia a Romina (borrador día 0)

**Status:** borrador. Sonnet revisa; Carlos lo prueba como humano el día 0.
**Nada toca el servidor todavía.**
**Origen:** encargo COS-200 (evento Buzz `d016a551cb012dd7dd1992e27c3e9bba60af1641f4bfd367b68b4729026988ab`).
**Skill:** `skills/onboarding/entrevista-inicial/SKILL.md` (este branch).
**Canal del cliente:** Urbanika Romina (`3db54844-3344-41ac-b26d-c473e9299bb1`).

---

## Cómo leer este archivo

Cada bloque `**Mia → Romina**` es **un mensaje** que Mia manda por WhatsApp,
tal como Romi lo ve en el cel. Cada bloque `**Romina → Mia**` es la
respuesta esperada (plantilla; Mia acepta lo que Romi diga). Los corchetes
`[así]` son el lugar donde va la respuesta real.

Mia espera la respuesta antes de mandar la siguiente pregunta. Una pregunta
por mensaje. Nada de bundles.

Mia escribe en **español** porque es la lengua de Romi (vive en Miami pero
su canal operativo con nosotros es en español). Si Romi contesta en inglés
o pide cambiar, Mia se adapta y guarda `principal_language` con lo que
Romina decida.

---

## Mensaje 1 — Apertura

**Mia → Romina**

> Hola Romi 👋 Soy Mia, tu bot de Urbanika Shades.
>
> Antes de empezar a contestar clientes contigo, quiero conocerte un poco.
> Te voy a hacer 10 preguntas, una por mensaje.
> Cuando quieras parar, manda `PARAR` y lo dejamos aquí.

---

## Pregunta 1 — Cómo le gusta que le hablen a ella

**Mia → Romina**

> Pregunta 1 de 10.
> ¿Cómo te gusta que te hable yo? ¿De tú o de usted? ¿Romina, Romi, Ma, o cómo te digo?

**Romina → Mia**

> [así: "de tú, dime Romi", "de usted, por favor", "de tú pero llámame Ma", etc.]

Mia guarda: `principal_address_style = <lo que Romi dijo>`.

---

## Pregunta 2 — Idioma del día a día

**Mia → Romina**

> Pregunta 2 de 10.
> Entre tú y yo, ¿en qué idioma me hablas? ¿Español, inglés, o mezclamos?
> Y si un cliente tuyo me escribe en el otro idioma, ¿le contesto en el suyo o en uno fijo?

**Romina → Mia**

> [así: "en español conmigo, con clientes en inglés", "todo en inglés", "mezcla como caiga", etc.]

Mia guarda: `principal_language = <lo que Romi dijo>`.

---

## Pregunta 3 — Tono con los clientes

**Mia → Romina**

> Pregunta 3 de 10.
> Cuando le escribo a tus clientes, ¿qué tono uso? ¿Cercano, profesional, relajado?
> ¿Pongo emojis o mejor no?

**Romina → Mia**

> [así: "cercano con un emoji de vez en cuando", "bien profesional, cero emojis", etc.]

Mia guarda: `customer_tone = <lo que Romi dijo>`.

---

## Pregunta 4 — Horario de atención

**Mia → Romina**

> Pregunta 4 de 10.
> ¿Cuál es tu horario de atención al cliente?
> Por ejemplo: lunes a viernes de 9 a 6, sábados de 10 a 2.
> Dime los días y las horas.

**Romina → Mia**

> [así: "L-V 9-6, S 10-2", "solo L-V 10-5", etc.]

Mia guarda: `principal_hours = <lo que Romi dijo>`.

---

## Pregunta 5 — Zona horaria

**Mia → Romina**

> Pregunta 5 de 10.
> Ese horario, ¿es hora de Miami (EST)? ¿O usas otra hora?

**Romina → Mia**

> [así: "Miami, hora del este", "Miami pero en verano uso EDT", etc. Si Romi no sabe, "sin dato".]

Mia guarda: `principal_timezone = <lo que Romi dijo>` (o `sin dato` si Romi no sabe).

---

## Pregunta 6 — A quién avisar si hay lío

**Mia → Romina**

> Pregunta 6 de 10.
> Si un cliente se queja fuerte, pide descuento que no estaba planeado, o se sale del flujo,
> ¿a quién le aviso y por dónde? Dime nombre y cómo contactarlo (teléfono, WhatsApp, correo).

**Romina → Mia**

> [así: "a mí directo por WhatsApp al +1 ...", "a Giovanna por correo g@...", etc.]

Mia guarda: `escalation_contact = <lo que Romi dijo>`.

---

## Pregunta 7 — Qué recordar siempre

**Mia → Romina**

> Pregunta 7 de 10.
> ¿Qué quieres que recuerde SIEMPRE de ti o de Urbanika?
> Tu apodo, el nombre de tu negocio completo, algo que no se me pueda olvidar.

**Romina → Mia**

> [así: "que el negocio se llama Urbanika Shades y está en Miami Beach", "mi apodo es Ma", etc.]

Mia guarda: `always_remember = <lo que Romi dijo>`.

---

## Pregunta 8 — Qué NO hacer sola

**Mia → Romina**

> Pregunta 8 de 10.
> ¿Qué cosas NO quieres que haga sola sin preguntarte antes?
> Como descuentos, excepciones de precio, cancelar citas, prometer plazos cortos, lo que sea.
> Dime la regla.

**Romina → Mia**

> [así: "nada de descuentos sin mi OK", "puedo mover una cita ±2h pero cancelar no", etc.]

Mia guarda: `never_alone = <lo que Romi dijo>`.

---

## Pregunta 9 — Qué NUNCA compartir con un cliente

**Mia → Romina**

> Pregunta 9 de 10.
> ¿Hay algo que NUNCA deba compartir con un cliente?
> Como precios de costo, nombres de proveedores, datos tuyos personales, comisiones.

**Romina → Mia**

> [así: "precios de costo nunca", "proveedores solo si pregunto yo antes", etc.]

Mia guarda: `never_share = <lo que Romi dijo>`.

---

## Pregunta 10 — Qué hago si no sé la respuesta

**Mia → Romina**

> Pregunta 10 de 10.
> Si un cliente me pregunta algo que no sé, ¿qué hago?
> ¿Le digo "déjame chequear y te aviso", te lo paso a ti directo, o le digo que no sé?

**Romina → Mia**

> [así: "dile que chequeo y aviso en X min", "pásamelo directo a mí", etc.]

Mia guarda: `unknown_default = <lo que Romi dijo>`.

---

## Mensaje final — Resumen de 5 líneas para confirmar

Después de la pregunta 10, Mia manda **un solo mensaje** con este resumen en
5 líneas. Romina confirma o corrige. Si corrige, Mia actualiza la entrada de
memoria correspondiente **antes** de contestar.

**Mia → Romina**

> Listo, esas son las 10. Te dejo el resumen en 5 líneas — dime si está bien o si cambio algo:
>
> 1. Te hablo de [P1: estilo + nombre] y en [P2: idioma].
> 2. A tus clientes les hablo en tono [P3].
> 3. Tu horario de atención es [P4], hora [P5].
> 4. Si hay lío, aviso a [P6].
> 5. Lo que NO hago sola: [P8]. Lo que NUNCA comparto: [P9].
>
> ¿Le doy o le cambio algo?

**Romina → Mia**

> [así: "dale", "perfecto", "cambia la línea 3 a ...", etc.]

Si Romina confirma, Mia manda una sola línea de cierre y la entrevista
termina. No vuelve a preguntar nada de esto en esta sesión.

**Mia → Romina (si Romina confirma)**

> Listo. Cuando me escribas "oye Samy/Mia, ya estás en vivo", arranco con los clientes.

(Nota: el nombre de cierre es "Samy" si la Mia/Samy de Urbanika ya fue
renombrada en GHL el 28-sep; si todavía dice "Maya", usar el nombre que
corresponda en producción al momento del día 0.)

---

## Datos confirmados del canal Urbanika Romina (solo referencia, NO se inventan)

Esto NO se manda a Romina en la entrevista. Es el contexto que ya conocemos
del canal Buzz y que la skill usa como "dato previo" — pero la entrevista
se las pregunta igual, porque Romina puede haber cambiado de opinión o el
dato puede estar desactualizado.

| Dato | Valor | Fuente |
|------|-------|--------|
| Negocio | Urbanika Shades | canal |
| Ubicación | Miami Beach, FL 33139 | canal (form leads) |
| Tel principal WA | +1 645-215-8835 (Twilio, "Temporary") | canal |
| Tel Romina (port pending desde 21-sep) | +1 786-361-9747 | canal |
| Email | urbanika.shades@gmail.com | canal |
| Servicios | Motorized shades, draperies, condo/casa/oficina | canal |
| GHL location | `9TMhXU1DRr4b0sM6Ftsp` | canal |
| VPS | `vps-435f1705` (40.160.89.94), `/opt/romina-fleet` | canal |
| Equipo mencionado | Giovanna, Santiago (pruebas/concierge) | canal |
| Agente voz | Samy EN + Samy ES | canal (28-sep) |
| Agente SMS (wa) | renombrado a Samy el 28-sep (antes Maya) | canal (28-sep) |
| WhatsApp histórico | 7172 — nunca se conectó a GHL | canal (28-sep) |

Sin dato a la hora de este borrador: horario exacto de Romina, idioma
preferido con Mia, escalación preferida (¿a ella misma o a alguien del
equipo?), reglas de descuento, política de datos sensibles, política de
"no sé".

---

## Lo que falta para que esto deje de ser borrador

1. Sonnet revisa este archivo + `SKILL.md` + `SOUL_ADDENDUM_MIA.md`.
2. Carlos lo prueba como humano (responde las 10 preguntas desde su cel).
3. Si pasa, se mergea al SOUL real de Mia en el perfil backend (la línea
   del addendum) y la skill queda disponible en el skill manager de Mia.
4. Mia corre la entrevista con Romina en producción **solo cuando** Carlos
   dé el GO explícito. Antes de ese GO, ningún cambio toca el servidor.