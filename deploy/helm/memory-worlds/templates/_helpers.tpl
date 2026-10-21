{{- define "memory-worlds.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "memory-worlds.fullname" -}}
{{- $name := "" -}}
{{- if .Values.fullnameOverride -}}
{{- $name = .Values.fullnameOverride -}}
{{- else if contains (include "memory-worlds.name" .) .Release.Name -}}
{{- $name = .Release.Name -}}
{{- else -}}
{{- $name = printf "%s-%s" .Release.Name (include "memory-worlds.name" .) -}}
{{- end -}}
{{- if not (regexMatch "^[a-z]" $name) -}}
{{- $name = printf "mw-%s" $name -}}
{{- end -}}
{{- if gt (len $name) 48 -}}
{{- printf "%s-%s" ($name | trunc 39 | trimSuffix "-") ($name | sha256sum | trunc 8) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- end -}}
{{- define "memory-worlds.validate" -}}
{{- if and .Values.falkordb.enabled .Values.persistence.existingClaim .Values.falkordb.persistence.existingClaim -}}
{{- if eq .Values.persistence.existingClaim .Values.falkordb.persistence.existingClaim -}}
{{- fail "API and FalkorDB require separate existing PVCs" -}}
{{- end -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
{{- range .Values.ingress.hosts -}}
{{- $host := . -}}
{{- $covered := false -}}
{{- range $.Values.ingress.tls -}}
{{- if has $host .hosts -}}{{- $covered = true -}}{{- end -}}
{{- end -}}
{{- if not $covered -}}{{- fail (printf "ingress host %s requires an exact entry in ingress.tls hosts" $host) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- define "memory-worlds.selectorLabels" -}}
app.kubernetes.io/name: {{ include "memory-worlds.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{- define "memory-worlds.labels" -}}
{{ include "memory-worlds.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "memory-worlds.image" -}}
{{- if .digest -}}
{{- printf "%s:%s@%s" .repository .tag .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}
{{- define "memory-worlds.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "memory-worlds.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
