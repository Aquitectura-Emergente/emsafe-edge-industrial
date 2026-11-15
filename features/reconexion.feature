# language: es
Característica: Continuidad de las lecturas sin conexión

  Escenario: Las lecturas pendientes se publican al volver el broker
    Dado que el broker MQTT no está disponible
    Cuando el Edge recibe 50 lecturas del dosímetro
    Entonces las 50 lecturas quedan pendientes en el outbox
    Cuando el broker vuelve a estar disponible
    Entonces el Edge publica las 50 lecturas en orden
    Y las marca como publicadas solo después del PUBACK

  Escenario: El dosímetro reenvía una lectura ya recibida
    Dado que el Edge ya recibió la lectura con secuencia 10
    Cuando el dosímetro vuelve a enviar la lectura con secuencia 10
    Entonces el outbox contiene una sola lectura con secuencia 10
