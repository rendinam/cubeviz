import os

import numpy as np
from glue.core import Subset
from qtpy.QtCore import QThread, Signal
from qtpy.QtWidgets import QApplication, QDialog
from qtpy.uic import loadUi
from spectral_cube import BooleanArrayMask, SpectralCube

from specviz.core.events import dispatch

__all__ = ['SpectralOperationHandler']

UI_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       '..', 'data', 'ui'))


class SpectralOperationHandler(QDialog):
    """
    Widget to handle user interactions with operations that are communicated
    from the SpecViz viewer. This is built to work with 
    :func:`~spectral_cube.SpectralCube.apply_function` method by passing in a 
    callable :class:`specviz.analysis.operations.FunctionalOperation` object.

    Attributes
    ----------
    data : :class:`~glue.core.data.Data`
        Glue data object on which the spectral operation will be performed.
    function : :class:`specviz.analysis.operations.FunctionalOperation`
        Python class instance whose `call` function will be performed on the 
        :class:`~spectral_cube.SpectralCube` object.
    """
    def __init__(self, data, function, *args, **kwargs):
        super(SpectralOperationHandler, self).__init__(*args, **kwargs)
        self.data = data
        self.function = function
        self.component_id = self.data.component_ids()[0]
        self._operation_thread = None

        self.setup_ui()
        self.setup_connections()

    def setup_ui(self):
        """Setup the PyQt UI for this dialog."""
        # Load the ui dialog
        loadUi(os.path.join(UI_PATH, "apply_operation.ui"), self)

        # Hide the progress bar initially
        self.progress_bar.hide()
        self.adjustSize()

        component_ids = [str(i) for i in self.data.component_ids()]

        # Populate combo box
        self.data_component_combo_box.addItems(component_ids)

    def setup_connections(self):
        """Setup signal/slot connections for this dialog."""
        # When a data component is selected, update the data object reference
        self.data_component_combo_box.currentIndexChanged.connect(
            self.on_index_changed)

        # If the abort button is clicked, attempted to stop execution
        self.abort_button.clicked.connect(self.on_aborted)

    def _compose_cube(self):
        """
        Create a :class:`~spectral_cube.SpectralCube` from a Glue data 
        component.
        """
        if issubclass(self.data.__class__, Subset):
            wcs = self.data.data.coords.wcs
            data = self.data.data
            mask = self.data.to_mask()
        else:
            wcs = self.data.coords.wcs
            data = self.data
            mask = np.ones(data.shape).astype(bool)

        mask = BooleanArrayMask(mask=mask, wcs=wcs)

        return SpectralCube(data[self.component_id], wcs=wcs, mask=mask)

    def on_index_changed(self, index):
        """Called when the index of the component combo box has changed."""
        self.component_id = self.data.component_ids()[index]

    def accept(self):
        """Called when the user clicks the "Okay" button of the dialog."""
        # Show the progress bar and abort button
        self.progress_bar.show()
        self.abort_button.setEnabled(True)

        self._operation_thread = OperationThread(self._compose_cube(),
                                                 function=self.function)

        self._operation_thread.finished.connect(self.on_finished)
        self._operation_thread.status.connect(self.on_status_updated)
        self._operation_thread.start()

    def on_aborted(self):
        """Called when the user aborts the operation."""
        self._operation_thread.abort()
        self.progress_bar.setValue(0)

        # Hide the progress bar and abort button
        self.progress_bar.hide()
        self.abort_button.setEnabled(False)

    def on_status_updated(self, value):
        """
        Called when the status of the operation has been updated. This can be
        optionally be passed a value to use as the new progress bar value.

        Attributes
        ----------
        value : float
            The value passed to the :class:`~qtpy.QtWidgets.QProgressBar`
            instance.
        """
        self.progress_bar.setValue(value * 100)

    def on_finished(self, data):
        """
        Called when the `QThread` has finished performing the operation on the
        `SpectralCube` object.

        Attributes
        ----------
        data : ndarray
            The result of the operation performed on the `SpectralCube` object.
        """
        component_name = "{} [Spectrally Smoothed]".format(self.component_id)

        comp_count = len([x for x in self.data.component_ids()
                          if component_name in str(x)])

        if comp_count > 0:
            component_name = "{} {}".format(component_name, comp_count)

        self.data.add_component(data, component_name)

        super(SpectralOperationHandler, self).accept()


class SimpleProgressTracker():
    """
    Simple container object to track the progress of an operation occuring in a
    :class:`~qtpyt.QtCore.QThread` instance. It is designed to be passed to
    :class:`~spectral_cube.SpectralCube` object to be called while performing
    operations.

    Attributes
    ----------
    total_value : float
        The maximum value of the progress.
    """
    def __init__(self, total_value):
        self._current_value = 0.0
        self._total_value = total_value
        self._abort_flag = False

    def __call__(self, value=None):
        self._current_value = value or self._current_value + 1

        if self._abort_flag:
            raise Exception("Process aborted.")

    @property
    def percent_value(self):
        """Return the completion amount as a percentage."""
        return self._current_value / self._total_value

    def abort(self):
        """
        Set the abort flag which will raise an error causing the operation
        to return immediately.
        """
        self._abort_flag = True


class OperationThread(QThread):
    """
    Thread in which an operation is performed on some 
    :class:`~spectral_cube.SpectralCube` object to ensure that the UI does not
    freeze while the operation is running.

    Attributes
    ----------
    cube_data : :class:`~spectral_cube.SpectralCube`
        The cube data on which the operation will be performed.
    function : callable
        The function-like callable used to perform the operation on the cube.
    """
    status = Signal(float)
    finished = Signal(np.ndarray)

    def __init__(self, cube_data, function, parent=None):
        super(OperationThread, self).__init__(parent)
        self._cube_data = cube_data
        self._function = function
        self._tracker = None

    def run(self):
        """Run the thread."""
        self._tracker = SimpleProgressTracker(
            self._cube_data.shape[1] * self._cube_data.shape[2])

        def progress_wrapper():
            self._tracker()
            self.status.emit(self._tracker.percent_value)

        new_data = self._cube_data.apply_function(
            self._function,
            spectral_axis=self._cube_data.spectral_axis,
            axis=0,
            keep_shape=True,
            update_function=progress_wrapper)

        self.finished.emit(new_data)

    def abort(self):
        """
        Abort the operation. Haults and returns immediately by raising an
        error.
        """
        self._tracker.abort()
