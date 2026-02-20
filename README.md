----- Das ist die erste, veröffentlichte Version der Dashcam. Sie enthält noch Bugs und Verbesserungsmöglichkeiten, die demnächst bearbeitet werden ---------  


# Kurzbeschreibung
<img src="OpenBikeDashcam.jpg" alt="Dashcam" width="800">
 
Die hier hoch geladene Dashcam ist als klassische Dashcam und speziell für Fahrräder konzipiert. Als Hardware wird ein Raspberry Pi 5 (CM5) verwendet. Darüber hinaus ist es aber auch eine Entwicklungsbasis für eure eigenen Projekte/Forschungsprojekte. Die Hardware kann selbst aufgebaut und angepasst werden. Die Daten können exportiert und analysiert werden. Alles, auch der Code ist offen zugänglich und kann für die eigenen Projekte verändert werden. Erweiterung durch Taster, Sensoren, etc. ist alles möglich. 

Das Projekt besteht aus einer Dashcam fürs Fahrrad, die Videos des rückwärtigen und seitlichen Verkehrs aufnimmt. Zusätzlich wird mit einem Abstandssensor der Überholabstand der vorbeifahrenden Autos ermittelt und im Video angezeigt. Über das Smartphone Display kann der letzte Überholabstand live überprüft werden. Folgende Funktionen sind bereits integriert: 

- Videoaufnahme in Sequenzen von einstellbarer Länge (z.B. 30Sekunden)
- Speicherung auf USB Stick nur, wenn während der Aufnahmezeit eines Segmentes der Sicherheitsabstand von 1,5m unterschritten wurde. sonst wird das Segment auch aus dem Zwischenspeicher gelöscht.  
- Einstellungen von zulässigem Abstand, Lenkerbreite, Länge der Videosequenzen, Versatz des Sensors von der Fahrradmitte ist über txt Dateien auf dem USB stick oder über die WebApp mögich
- Sollten auf dem USB Stick weniger als 100mb Speicherplatz verbleiben, wird das letzte Video gelöscht (Status LED zeigt das durch blau farbe an)
- Zu jedem Video wird eine CSV Datei mit GPS Daten, Zeit und Abstand gespeichert.

Dafür, was mit den Videos passiert, ist der Nutzer selbst verantwortlich. 


# Ausblick: Plan für Funktionen, die in Zukunft bearbeitet werden  
- ich arbeite aktuell an einem Gehäuse mit Schnellwechseladapter für die Sattelstange.
- Upgrade auf richtiges UPS (Energieversorgung) statt einer Power bank. 
- weitere Fahrradcomputer-Funktionen in der WebApp (Max-Geschwindigkeit, Durchschnittsgeschwindigkeit, Fahrzeit, Fahrstecke in km. Alles seit dem letzten Programmstart und seit Inbetriebnahme der Dashcam)

# Lizenz
Software: GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007  
Hardware: CERN Open Hardware Licence Version 2 - Strongly Reciprocal

