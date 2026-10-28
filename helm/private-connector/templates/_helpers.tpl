{{- define "connector.name" -}}
{{- printf "%s-connector" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "connector.labels" -}}
app.kubernetes.io/name: private-connector
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{- define "connector.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ default .Chart.AppVersion .Values.image.tag }}
{{- end -}}
{{- end -}}
