# Semantischer Mediator (BIBA) als zweiter Dienst neben den A2A-Agenten.
#
# Der Mediator ist ein Spring-Boot-Dienst (Java 8) aus einem eigenen Repository:
#   C:\Users\fma\git\semanticmediator
# Sein Quellcode wird hier NICHT mitgebaut. Das JAR entsteht dort mit
#
#   mvn clean package            (das `clean` ist Pflicht, siehe dessen CLAUDE.md)
#
# und wird anschliessend hierher kopiert:
#
#   copy ..\..\git\semanticmediator\target\semanticmediator.jar mediator\
#
# Der Grund fuer die Trennung: Der Mediator haengt an internen SNAPSHOT-Artefakten
# (de.biba.reasoning.mediator, de.biba.wrapper.*), die nur aus dem BIBA-Netz
# erreichbar sind. Ein `mvn package` im Container wuerde auf jedem anderen
# Rechner scheitern. Wer Zugriff auf die Registry hat, nimmt statt dieses
# Dockerfiles einfach das fertige Image - siehe docker-compose.yml.
FROM eclipse-temurin:8-jre

WORKDIR /

COPY semanticmediator.jar /semanticmediator.jar

# Im Image steht der Port auf 3053 (src/main/resources/application.yml). Hier
# 8081, damit er zu dem passt, was Agent H in SEMANTIC_MEDIATOR_URL erwartet.
# Spring liest SERVER_PORT als Ueberschreibung von server.port.
ENV SERVER_PORT=8081

EXPOSE 8081

# Healthcheck ohne curl oder wget - beide sind in eclipse-temurin nicht
# garantiert vorhanden. `echo > /dev/tcp/host/port` ist eine Bash-Eigenheit, die
# einen TCP-Verbindungsversuch macht und bei Misserfolg fehlschlaegt. Deshalb
# ausdruecklich `bash` und nicht die Shell-Form (die benutzt sh, und das kann es
# nicht). Als Bereitschaftssignal taugt der offene Port, weil Spring Boot den
# Port erst ganz am Ende oeffnet - nach dem Laden der Wrapper aus der config.xml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD ["bash", "-c", "echo > /dev/tcp/127.0.0.1/$SERVER_PORT"]

CMD ["java", "-jar", "/semanticmediator.jar"]
