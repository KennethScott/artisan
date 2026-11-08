"""Unit tests for artisanlib.webapi_dialog (local control API settings dialog)."""

import sys
from collections.abc import Generator
from typing import cast
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QApplication

from artisanlib.webapi_dialog import WebAPISettingsDlg


@pytest.fixture(scope='session')
def qapp() -> Generator[QApplication, None, None]:
    if not QApplication.instance():
        app = QApplication(sys.argv)
        yield app
    else:
        yield cast(QApplication, QApplication.instance())


@pytest.fixture
def aw() -> Mock:
    aw = Mock()
    aw.qmc.mode = 'F'
    aw.qmc.etypes = ['Air', 'Drum', 'Damper', 'Burner', '--']
    aw.eventslidermin = [0, 0, 0, 0]
    aw.eventslidermax = [100, 100, 100, 100]
    aw.WebAPIMinInterval = 1.0
    aw.WebAPIMaxStep = 0
    aw.WebAPIMaxSVStep = 0.0
    aw.WebAPIWatchdogTimeout = 0.0
    aw.WebAPIWatchdogSlider = -1
    aw.WebAPIWatchdogValue = 0
    aw.WebAPIWatchdogPIDOff = False
    return aw


@pytest.mark.usefixtures('qapp')
def test_dialog_loads_current_settings(aw:Mock) -> None: # pylint: disable=redefined-outer-name
    aw.WebAPIWatchdogSlider = 3
    aw.WebAPIMaxSVStep = 15.0
    dlg = WebAPISettingsDlg(None, aw)
    assert dlg.fallbackSlider.currentData() == 3
    assert dlg.fallbackSlider.currentText() == 'Burner'
    assert dlg.fallbackValue.isEnabled()
    assert dlg.maxSVStep.value() == 15.0
    assert dlg.maxSVStep.suffix() == ' °F'
    dlg.deleteLater()


@pytest.mark.usefixtures('qapp')
def test_dialog_none_fallback_disables_value(aw:Mock) -> None: # pylint: disable=redefined-outer-name
    dlg = WebAPISettingsDlg(None, aw)
    assert dlg.fallbackSlider.currentData() == -1
    assert not dlg.fallbackValue.isEnabled()
    dlg.deleteLater()


@pytest.mark.usefixtures('qapp')
def test_dialog_accept_writes_settings(aw:Mock) -> None: # pylint: disable=redefined-outer-name
    dlg = WebAPISettingsDlg(None, aw)
    dlg.minInterval.setValue(2.5)
    dlg.maxStep.setValue(20)
    dlg.maxSVStep.setValue(10)
    dlg.fallbackSlider.setCurrentIndex(dlg.fallbackSlider.findData(3))
    dlg.fallbackValue.setValue(0)
    dlg.watchdogTimeout.setValue(30)
    dlg.watchdogPIDOff.setChecked(True)
    dlg.accept()
    assert aw.WebAPIMinInterval == 2.5
    assert aw.WebAPIMaxStep == 20
    assert aw.WebAPIMaxSVStep == 10.0
    assert aw.WebAPIWatchdogSlider == 3
    assert aw.WebAPIWatchdogValue == 0
    assert aw.WebAPIWatchdogTimeout == 30.0
    assert aw.WebAPIWatchdogPIDOff is True


@pytest.mark.usefixtures('qapp')
def test_dialog_cancel_keeps_settings(aw:Mock) -> None: # pylint: disable=redefined-outer-name
    dlg = WebAPISettingsDlg(None, aw)
    dlg.maxStep.setValue(50)
    dlg.reject()
    assert aw.WebAPIMaxStep == 0
