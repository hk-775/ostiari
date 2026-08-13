{{- define "ostiari-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ostiari-gateway.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "ostiari-gateway.validateProduction" -}}
{{- if or (eq .Values.gateway.env "production") (eq .Values.gateway.env "prod") }}
{{- $_ := required "image.digest is required for production" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-fA-F0-9]{64}$" .Values.image.digest) -}}
{{- fail "image.digest must be a sha256: digest with 64 hexadecimal characters" -}}
{{- end -}}
{{- $_ := required "gateway.tenantId is required for production" .Values.gateway.tenantId -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" .Values.gateway.tenantId) -}}
{{- fail "gateway.tenantId must be 1-64 letters, digits, dots, underscores, or hyphens" -}}
{{- end -}}
{{- $_ := required "gateway.oidcIssuer is required for production" .Values.gateway.oidcIssuer -}}
{{- if not (hasPrefix "https://" .Values.gateway.oidcIssuer) -}}
{{- fail "gateway.oidcIssuer must use HTTPS in production" -}}
{{- end -}}
{{- $_ := required "gateway.oidcAudience is required for production" .Values.gateway.oidcAudience -}}
{{- if lt (int .Values.gateway.rateLimitRpm) 1 -}}
{{- fail "gateway.rateLimitRpm must be a positive integer for production" -}}
{{- end -}}
{{- $_ := required "secrets.existingSecret is required for production" .Values.secrets.existingSecret -}}
{{- if not .Values.redis.enabled -}}
{{- fail "redis.enabled must be true for production" -}}
{{- end -}}
{{- $_ := required "redis.urlSecretKey is required for production" .Values.redis.urlSecretKey -}}
{{- if not .Values.gateway.failClosedOnControlPlaneLoss -}}
{{- fail "gateway.failClosedOnControlPlaneLoss must be true for production" -}}
{{- end -}}
{{- if eq .Values.payments.x402Mode "simulated" -}}
{{- fail "payments.x402Mode may not be simulated in production" -}}
{{- end -}}
{{- end -}}
{{- end }}
