# Propuesta normativa: bloqueo en vivo de transmisiones ilegales, con juez de turno

**Problema.** Un partido dura dos horas y un sitio pirata cambia de dominio o de servidor en minutos. Hoy el bloqueo depende de una sola fiscalía y un solo juzgado, la orden llega por cámaras de proveedores y no hay reglas claras para el daño a terceros. Resultado: se bloquea tarde, o se bloquea de más.

**Qué debería establecer una ley o una resolución federal:**

1. **Orden marco y jueces de turno.**
   - Una resolución crea un régimen federal de turnos: cada juzgado designa un juez de guardia por franja horaria, publicada en un registro oficial.
   - El juez ingresa con credencial personal y **solo firma mientras está de turno**. Al cambiar el turno, su credencial deja de valer y lo pendiente pasa al juez entrante.
   - **La decisión del juez es siempre manual y queda firmada** por cada expediente y reclamo; nadie firma en su nombre. El sistema solo asiste en el armado de evidencia, la verificación de reglas, el cálculo del vencimiento y el envío/reintentos tras la firma judicial.
2. **Registro único de proveedores de internet (ISP).**
   - ENACOM mantiene un registro obligatorio y actualizado: contacto técnico, tipos de bloqueo que soporta (IP, dominio) y cantidad de clientes.
   - Todo ISP debe recibir órdenes por una API estándar y **confirmar la aplicación en un plazo máximo** fijado por la norma (por ejemplo, 10 minutos para los grandes y 30 para los chicos).
   - Si un ISP falla, el sistema reintenta automáticamente y registra la demora. No hace falta pasar por cámaras ni por correo.
3. **Evidencia mínima verificable.** Ningún pedido llega al juez sin: marca de agua que pruebe que es la señal oficial del partido, verificación de que no es un licenciatario autorizado, servidor (host, IP y proveedor) y sellos de tiempo y de integridad (hash) de la captura. Cualquier alteración posterior debe poder detectarse.
4. **Prohibición de bloquear por IP si la IP es compartida.**
   - Si una IP aloja otros sitios legítimos (CDN o hosting compartido, según DNS pasivo), el bloqueo se pide **solo por dominio**.
   - El bloqueo por IP solo se permite cuando la IP está 100 % dedicada al stream pirata.
   - Si una IP bloqueada pasa a ser compartida, se revisa de oficio.
5. **Caducidad automática.** Todo bloqueo por IP vence solo al terminar el partido; el vencimiento va escrito en la orden firmada. Prorrogar exige una nueva orden. Los bloqueos por dominio duran lo que diga la orden, con tope fijo.
6. **Reclamos de sitios afectados.** Cualquier sitio puede reclamar por un canal público. El juez de turno debe resolver dentro de un plazo máximo de **15 minutos**: levanta la orden con su firma y, si corresponde, la reemplaza por un bloqueo más preciso. El reclamo y su resolución quedan en el registro.
7. **Auditoría y transparencia.**
   - Cada paso (detección, firma, envío, confirmación, reclamo, vencimiento) se guarda en un registro que no se puede reescribir.
   - Un registro público muestra qué se bloqueó, desde cuándo, hasta cuándo y por orden de quién.
   - Los titulares de derechos responden por los pedidos hechos de mala fe o sin evidencia.

**Por qué funciona.** El juez decide con evidencia ordenada y en minutos; los proveedores reciben una orden única y clara; el daño a terceros se limita por regla y no por criterio caso a caso; y todo lo que se bloquea vence solo y queda a la vista del público.

*Los datos de la demostración son ficticios. Este texto es una propuesta técnica, no asesoramiento legal.*
