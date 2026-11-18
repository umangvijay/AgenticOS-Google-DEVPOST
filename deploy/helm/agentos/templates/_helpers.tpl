{{- define "agentos.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentos.fullname" -}}
{{- printf "%s" (default (include "agentos.name" .) .Values.fullnameOverride) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentos.image" -}}
{{- $registry := .global.imageRegistry -}}
{{- $sep := ternary "/" "" (ne $registry "") -}}
{{- /* §27.D8: a digest reference pins the deployed bytes — required for
       release qualification. `imageDigest` values are sha256:… strings;
       when absent (dev profile) the tag form is used and the deployed
       digest readback stage will fail it. */ -}}
{{- $digest := index . "digest" -}}
{{- if $digest -}}
{{- printf "%s%s%s@%s" $registry $sep .image $digest -}}
{{- else -}}
{{- printf "%s%s%s:%s" $registry $sep .image .global.imageTag -}}
{{- end -}}
{{- end -}}

{{- define "agentos.labels" -}}
app.kubernetes.io/name: {{ include "agentos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "agentos.storageEnv" -}}
- name: STORAGE_BACKEND
  value: {{ .Values.storage.backend | quote }}
{{- if eq .Values.storage.backend "postgres" }}
# Phase 22 (found by the kind install): the application reads
# settings.POSTGRES_DSN — DATABASE_URL alone left pods on the localhost
# default. Inject the app's actual setting name (keep DATABASE_URL for
# 12-factor convention).
- name: POSTGRES_DSN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.storage.postgres.existingSecret }}
      key: {{ .Values.storage.postgres.secretKey }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.storage.postgres.existingSecret }}
      key: {{ .Values.storage.postgres.secretKey }}
{{- else }}
- name: GOOGLE_CLOUD_PROJECT
  value: {{ required "firestore project required" .Values.storage.firestore.project | quote }}
- name: FIRESTORE_DATABASE_ID
  value: {{ .Values.storage.firestore.databaseId | quote }}
{{- end }}
- name: MESSAGE_BUS_TRANSPORT
  value: {{ .Values.bus.transport | quote }}
{{- if eq .Values.bus.transport "nats" }}
- name: NATS_SERVERS
  value: {{ .Values.bus.nats.servers | quote }}
{{- end }}
{{- end -}}
