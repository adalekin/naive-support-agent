{{- define "assistant.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "assistant.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "assistant.selectorLabels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "assistant.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- include "assistant.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "assistant.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{- define "assistant.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "assistant.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "assistant.secretName" .root }}
      key: {{ .name }}
{{- end -}}

{{/* Environment of the image: used by the assistant and by the migrations Job. */}}
{{- define "assistant.env" -}}
{{- $s := .Values.settings -}}
- name: TELEGRAM_BOT_USERNAME
  value: {{ required "settings.telegram.botUsername is required" $s.telegram.botUsername | quote }}
- name: TELEGRAM_EGRESS
  value: {{ required "settings.telegram.egress is required: direct or proxy" $s.telegram.egress | quote }}
- name: MODEL_EGRESS
  value: {{ required "settings.model.egress is required: direct or proxy" $s.model.egress | quote }}
- name: TELEGRAM_GROUPS
  {{- /* Chat numbers are large: YAML reads them as floats, so each is printed as a whole number. */}}
  value: {{ $groups := list }}{{ range $s.telegram.groups }}{{ $groups = append $groups (printf "%d" (int64 .)) }}{{ end }}{{ $groups | join "," | quote }}
- name: LOCALE
  value: {{ required "settings.locale is required: ru or en" $s.locale | quote }}
- name: OIDC_ISSUER
  value: {{ required "settings.oidc.issuer is required" $s.oidc.issuer | quote }}
- name: OIDC_CLIENT_ID
  value: {{ required "settings.oidc.clientId is required" $s.oidc.clientId | quote }}
- name: OIDC_ROLES_CLAIM
  value: {{ required "settings.oidc.rolesClaim is required" $s.oidc.rolesClaim | quote }}
- name: ROLE_SUPPORT
  value: {{ required "settings.roles.support is required" $s.roles.support | quote }}
- name: ROLE_OWNER
  value: {{ required "settings.roles.owner is required" $s.roles.owner | quote }}
- name: ROLE_ESCALATION
  value: {{ required "settings.roles.escalation is required" $s.roles.escalation | quote }}
- name: ROLE_ANALYTICS
  value: {{ required "settings.roles.analytics is required" $s.roles.analytics | quote }}
- name: ANALYTICS_ROLES_CLAIM
  value: {{ required "settings.roles.analyticsClaim is required" $s.roles.analyticsClaim | quote }}
- name: MODEL
  value: {{ required "settings.model.name is required" $s.model.name | quote }}
- name: MODEL_EFFORT
  value: {{ required "settings.model.effort is required" $s.model.effort | quote }}
- name: CASE_TOKEN_BUDGET
  value: {{ required "settings.model.caseTokenBudget is required" $s.model.caseTokenBudget | quote }}
- name: FOLLOWUP_INTERVAL_MINUTES
  value: {{ required "settings.followUpIntervalMinutes is required" $s.followUpIntervalMinutes | quote }}
- name: CASE_RETENTION_DAYS
  value: {{ required "settings.caseRetentionDays is required" $s.caseRetentionDays | quote }}
- name: INSTRUCTIONS_DIR
  value: /instructions
- name: DB_HOST
  value: {{ required "settings.database.host is required" $s.database.host | quote }}
- name: DB_PORT
  value: {{ required "settings.database.port is required" $s.database.port | quote }}
- name: DB_NAME
  value: {{ required "settings.database.name is required" $s.database.name | quote }}
- name: DB_USER
  value: {{ required "settings.database.user is required" $s.database.user | quote }}
{{- range list "TELEGRAM_BOT_TOKEN" "OIDC_CLIENT_SECRET" "ANTHROPIC_API_KEY" "ENCRYPTION_KEY" "DB_PASSWORD" }}
{{ include "assistant.secretEnv" (dict "root" $ "name" .) }}
{{- end }}
{{- if eq $s.telegram.egress "proxy" }}
{{ include "assistant.secretEnv" (dict "root" $ "name" "TELEGRAM_PROXY_URL") }}
{{- end }}
{{- if eq $s.model.egress "proxy" }}
{{ include "assistant.secretEnv" (dict "root" $ "name" "MODEL_PROXY_URL") }}
{{- end }}
{{- end -}}
