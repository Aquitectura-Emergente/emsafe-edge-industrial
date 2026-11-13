"""Configuración por entorno: valores por defecto y errores claros."""
import unittest

from shared.config import EdgeSettings


class DefaultsTest(unittest.TestCase):
    def test_sin_variables_usa_los_defaults_documentados(self):
        s = EdgeSettings.from_env({})

        self.assertEqual(s.edge_node_id, "EDGE-PLANTA-01")
        self.assertEqual(s.organization_id, "org-demo")
        self.assertEqual(s.device_names, ("EMSafe-DOS-01",))
        self.assertEqual(s.default_threshold_ut, 300.0)
        self.assertEqual(s.saturation_ut, 790.0)
        self.assertEqual(s.mqtt_host, "localhost")
        self.assertEqual(s.mqtt_port, 1883)
        self.assertFalse(s.mqtt_tls)
        self.assertIsNone(s.mqtt_username)
        self.assertIsNone(s.mqtt_password)
        self.assertEqual(s.db_path, "edge_industrial.db")
        self.assertEqual(s.outbox_retention_days, 7)

    def test_retencion_del_outbox(self):
        self.assertEqual(EdgeSettings.from_env({"EMSAFE_OUTBOX_RETENTION_DAYS": "30"}).outbox_retention_days, 30)
        for raw in ("0", "-1", "siete"):
            with self.assertRaises(ValueError, msg=raw) as ctx:
                EdgeSettings.from_env({"EMSAFE_OUTBOX_RETENTION_DAYS": raw})
            self.assertIn("EMSAFE_OUTBOX_RETENTION_DAYS", str(ctx.exception))

    def test_variables_vacias_cuentan_como_no_definidas(self):
        s = EdgeSettings.from_env({"EMSAFE_MQTT_HOST": "  ", "EMSAFE_MQTT_PORT": ""})

        self.assertEqual(s.mqtt_host, "localhost")
        self.assertEqual(s.mqtt_port, 1883)


class ParsingTest(unittest.TestCase):
    def test_lee_todas_las_variables(self):
        s = EdgeSettings.from_env({
            "EMSAFE_EDGE_NODE_ID": "EDGE-PLANTA-07",
            "EMSAFE_ORGANIZATION_ID": "org-minera",
            "EMSAFE_DEFAULT_THRESHOLD_UT": "250.5",
            "EMSAFE_SATURATION_UT": "780",
            "EMSAFE_MQTT_HOST": "broker.local",
            "EMSAFE_MQTT_PORT": "8883",
            "EMSAFE_MQTT_TLS": "true",
            "EMSAFE_MQTT_USERNAME": "edge07",
            "EMSAFE_MQTT_PASSWORD": "secreto",
            "EMSAFE_DB_PATH": "/tmp/edge.db",
        })

        self.assertEqual(s.edge_node_id, "EDGE-PLANTA-07")
        self.assertEqual(s.organization_id, "org-minera")
        self.assertEqual(s.default_threshold_ut, 250.5)
        self.assertEqual(s.saturation_ut, 780.0)
        self.assertEqual(s.mqtt_host, "broker.local")
        self.assertEqual(s.mqtt_port, 8883)
        self.assertTrue(s.mqtt_tls)
        self.assertEqual(s.mqtt_username, "edge07")
        self.assertEqual(s.mqtt_password, "secreto")
        self.assertEqual(s.db_path, "/tmp/edge.db")

    def test_nombres_ble_separados_por_coma_sin_espacios_ni_repetidos(self):
        s = EdgeSettings.from_env(
            {"EMSAFE_DEVICE_NAMES": " EMSafe-DOS-01 , EMSafe-DOS-02,,EMSafe-DOS-01 "}
        )

        self.assertEqual(s.device_names, ("EMSafe-DOS-01", "EMSafe-DOS-02"))

    def test_lista_de_nombres_sin_nombres_validos_falla(self):
        with self.assertRaises(ValueError) as ctx:
            EdgeSettings.from_env({"EMSAFE_DEVICE_NAMES": " , ,"})
        self.assertIn("EMSAFE_DEVICE_NAMES", str(ctx.exception))

    def test_booleanos_aceptan_variantes_habituales(self):
        for raw in ("1", "true", "TRUE", "yes", "si"):
            self.assertTrue(EdgeSettings.from_env({"EMSAFE_MQTT_TLS": raw}).mqtt_tls, raw)
        for raw in ("0", "false", "no", "off"):
            self.assertFalse(EdgeSettings.from_env({"EMSAFE_MQTT_TLS": raw}).mqtt_tls, raw)

    def test_booleano_ambiguo_falla_en_vez_de_adivinar(self):
        with self.assertRaises(ValueError) as ctx:
            EdgeSettings.from_env({"EMSAFE_MQTT_TLS": "quizas"})
        self.assertIn("EMSAFE_MQTT_TLS", str(ctx.exception))


class ValidationTest(unittest.TestCase):
    def test_umbral_no_numerico_nombra_la_variable(self):
        with self.assertRaises(ValueError) as ctx:
            EdgeSettings.from_env({"EMSAFE_DEFAULT_THRESHOLD_UT": "alto"})
        self.assertIn("EMSAFE_DEFAULT_THRESHOLD_UT", str(ctx.exception))

    def test_umbral_nan_se_rechaza(self):
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_DEFAULT_THRESHOLD_UT": "nan"})

    def test_umbral_debe_quedar_bajo_la_saturacion(self):
        # Un umbral igual o mayor que la saturación nunca dispararía la alarma.
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_DEFAULT_THRESHOLD_UT": "790"})
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_DEFAULT_THRESHOLD_UT": "0"})

    def test_puerto_invalido(self):
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_MQTT_PORT": "abc"})
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_MQTT_PORT": "70000"})


class SecretsTest(unittest.TestCase):
    def test_las_credenciales_no_aparecen_en_repr(self):
        s = EdgeSettings.from_env(
            {"EMSAFE_MQTT_USERNAME": "edge07", "EMSAFE_MQTT_PASSWORD": "secreto-123"}
        )

        self.assertNotIn("secreto-123", repr(s))
        self.assertNotIn("edge07", repr(s))


if __name__ == "__main__":
    unittest.main()
