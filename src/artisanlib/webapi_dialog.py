#
# ABOUT
# Settings dialog for the local control API (see artisanlib/webapi.py)
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from typing import override, TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (QApplication, QWidget, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout)

from artisanlib.dialogs import ArtisanDialog
from artisanlib.webapi import FALLBACK_AUTO, FALLBACK_NONE

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import


class WebAPISettingsDlg(ArtisanDialog):
    """Safety settings of the local control API. They are read when the API starts,
    so the dialog is only offered while the API is off (a restart mid-roast would clear its locks)."""

    def __init__(self, parent:QWidget|None, aw:'ApplicationWindow') -> None:
        super().__init__(parent, aw)
        self.setWindowTitle(QApplication.translate('Form Caption', 'Local Control API Settings'))
        self.setModal(True)
        unit = f'°{aw.qmc.mode}'

        self.minInterval = QDoubleSpinBox()
        self.minInterval.setRange(0., 60.)
        self.minInterval.setDecimals(1)
        self.minInterval.setSingleStep(0.5)
        self.minInterval.setSuffix(' s')
        self.minInterval.setValue(aw.WebAPIMinInterval)
        self.minInterval.setToolTip(QApplication.translate('Tooltip',
            'Minimum time between two writes to the same slider, the SV or the PID (0: off)'))

        self.maxStep = QSpinBox()
        self.maxStep.setRange(0, 1000)
        self.maxStep.setSpecialValueText(QApplication.translate('Label', 'unlimited'))
        self.maxStep.setValue(aw.WebAPIMaxStep)
        self.maxStep.setToolTip(QApplication.translate('Tooltip', 'Largest slider change accepted in one write'))

        self.maxSVStep = QDoubleSpinBox()
        self.maxSVStep.setRange(0., 1000.)
        self.maxSVStep.setDecimals(1)
        self.maxSVStep.setSuffix(f' {unit}')
        self.maxSVStep.setSpecialValueText(QApplication.translate('Label', 'unlimited'))
        self.maxSVStep.setValue(aw.WebAPIMaxSVStep)
        self.maxSVStep.setToolTip(QApplication.translate('Tooltip', 'Largest PID set value change accepted in one write'))

        limitsLayout = QFormLayout()
        limitsLayout.addRow(QApplication.translate('Label', 'Min interval'), self.minInterval)
        limitsLayout.addRow(QApplication.translate('Label', 'Max slider step'), self.maxStep)
        limitsLayout.addRow(QApplication.translate('Label', 'Max SV step'), self.maxSVStep)
        limitsGroup = QGroupBox(QApplication.translate('GroupBox', 'Write Limits'))
        limitsGroup.setLayout(limitsLayout)

        self.fallbackSlider = QComboBox()
        self.fallbackSlider.addItem(QApplication.translate('ComboBox', 'Auto'), FALLBACK_AUTO)
        self.fallbackSlider.addItem(QApplication.translate('ComboBox', 'None'), FALLBACK_NONE)
        for n in range(4):
            self.fallbackSlider.addItem(str(aw.qmc.etypes[n]), n)
        self.fallbackSlider.setCurrentIndex(max(0, self.fallbackSlider.findData(aw.WebAPIWatchdogSlider)))
        self.fallbackSlider.currentIndexChanged.connect(self.fallbackSliderChanged)
        self.fallbackValue = QSpinBox()
        self.fallbackValue.setRange(-9999, 9999)
        self.fallbackValue.setValue(aw.WebAPIWatchdogValue)
        self.fallbackEquals = QLabel('=')
        self.fallbackInfo = QLabel()
        self.fallbackInfo.setWordWrap(True)
        fallbackLayout = QHBoxLayout()
        fallbackLayout.addWidget(self.fallbackSlider)
        fallbackLayout.addWidget(self.fallbackEquals)
        fallbackLayout.addWidget(self.fallbackValue)
        fallbackLayout.addStretch()
        fallbackVLayout = QVBoxLayout()
        fallbackVLayout.addLayout(fallbackLayout)
        fallbackVLayout.addWidget(self.fallbackInfo)
        self.fallbackSliderChanged()

        self.watchdogTimeout = QSpinBox()
        self.watchdogTimeout.setRange(0, 600)
        self.watchdogTimeout.setSuffix(' s')
        self.watchdogTimeout.setSpecialValueText(QApplication.translate('Label', 'off'))
        self.watchdogTimeout.setValue(int(round(aw.WebAPIWatchdogTimeout)))
        self.watchdogTimeout.setToolTip(QApplication.translate('Tooltip',
            'If the API client went silent this long during a roast it controlled, the fallback is applied'))
        self.watchdogPIDOff = QCheckBox(QApplication.translate('CheckBox', 'Switch PID off'))
        self.watchdogPIDOff.setChecked(aw.WebAPIWatchdogPIDOff)
        self.watchdogPIDOff.setToolTip(QApplication.translate('Tooltip', 'The watchdog also switches the PID off (STOP always does)'))

        safetyLayout = QFormLayout()
        safetyLayout.addRow(QApplication.translate('Label', 'Fallback'), fallbackVLayout)
        safetyLayout.addRow(QApplication.translate('Label', 'Watchdog'), self.watchdogTimeout)
        safetyLayout.addRow('', self.watchdogPIDOff)
        hint = QLabel(QApplication.translate('Label',
            'The fallback (typically heat to 0) is applied by STOP and by the watchdog.'))
        hint.setWordWrap(True)
        safetyVLayout = QVBoxLayout()
        safetyVLayout.addLayout(safetyLayout)
        safetyVLayout.addWidget(hint)
        safetyGroup = QGroupBox(QApplication.translate('GroupBox', 'Safety'))
        safetyGroup.setLayout(safetyVLayout)

        self.dialogbuttons.accepted.connect(self.accept)
        self.dialogbuttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(limitsGroup)
        layout.addWidget(safetyGroup)
        layout.addWidget(self.dialogbuttons, alignment=Qt.AlignmentFlag.AlignRight)
        self.setLayout(layout)

    @pyqtSlot(int)
    def fallbackSliderChanged(self, _:int = 0) -> None:
        n = self.fallbackSlider.currentData()
        explicit = isinstance(n, int) and 0 <= n < 4
        self.fallbackValue.setVisible(explicit)
        self.fallbackEquals.setVisible(explicit)
        if explicit:
            assert isinstance(n, int)
            self.fallbackValue.setRange(int(self.aw.eventslidermin[n]), int(self.aw.eventslidermax[n]))
            self.fallbackInfo.setText('')
        elif n == FALLBACK_AUTO:
            heat = self.heatSlider()
            if heat is None:
                self.fallbackInfo.setText(QApplication.translate('Label',
                    'No PID heat slider configured: STOP and the watchdog cannot cut heat'))
            else:
                self.fallbackInfo.setText(QApplication.translate('Label', 'PID heat slider: {0} = {1}').format(
                    self.aw.qmc.etypes[heat], int(self.aw.eventslidermin[heat])))
        else:
            self.fallbackInfo.setText(QApplication.translate('Label', 'STOP and the watchdog cannot cut heat'))
        self.fallbackInfo.setVisible(bool(self.fallbackInfo.text()))

    def heatSlider(self) -> int|None:
        target = int(self.aw.pidcontrol.pidPositiveTarget)
        return target - 1 if 1 <= target <= 4 else None

    @pyqtSlot()
    @override
    def accept(self) -> None:
        aw = self.aw
        aw.WebAPIMinInterval = float(self.minInterval.value())
        aw.WebAPIMaxStep = int(self.maxStep.value())
        aw.WebAPIMaxSVStep = float(self.maxSVStep.value())
        data = self.fallbackSlider.currentData()
        aw.WebAPIWatchdogSlider = data if isinstance(data, int) else FALLBACK_NONE
        aw.WebAPIWatchdogValue = int(self.fallbackValue.value())
        aw.WebAPIWatchdogTimeout = float(self.watchdogTimeout.value())
        aw.WebAPIWatchdogPIDOff = self.watchdogPIDOff.isChecked()
        super().accept()
