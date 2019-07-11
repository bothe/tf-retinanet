import tensorflow as tf


def filter_detections(
	boxes,
	classification,
	other                 = [],
	class_specific_filter = True,
	nms                   = True,
	score_threshold       = 0.05,
	max_detections        = 300,
	nms_threshold         = 0.5
):
	""" Filter detections using the boxes and classification values.
	Args
		boxes                 : Tensor of shape (num_boxes, 4) containing the boxes in (x1, y1, x2, y2) format.
		classification        : Tensor of shape (num_boxes, num_classes) containing the classification scores.
		other                 : List of tensors of shape (num_boxes, ...) to filter along with the boxes and classification scores.
		class_specific_filter : Whether to perform filtering per class, or take the best scoring class and filter those.
		nms                   : Flag to enable/disable non maximum suppression.
		score_threshold       : Threshold used to prefilter the boxes with.
		max_detections        : Maximum number of detections to keep.
		nms_threshold         : Threshold for the IoU value to determine when a box should be suppressed.
	Returns
		A list of [boxes, scores, labels, other[0], other[1], ...].
		boxes is shaped (max_detections, 4) and contains the (x1, y1, x2, y2) of the non-suppressed boxes.
		scores is shaped (max_detections,) and contains the scores of the predicted class.
		labels is shaped (max_detections,) and contains the predicted label.
		other[i] is shaped (max_detections, ...) and contains the filtered other[i] data.
		In case there are less than max_detections detections, the tensors are padded with -1's.
	"""
	def _filter_detections(scores, labels):
		# Threshold based on score.
		indices = tf.where(tf.keras.backend.greater(scores, score_threshold))

		if nms:
			filtered_boxes  = tf.gather_nd(boxes, indices)
			filtered_scores = tf.keras.backend.gather(scores, indices)[:, 0]

			# Perform NMS.
			nms_indices = tf.image.non_max_suppression(filtered_boxes, filtered_scores, max_output_size=max_detections, iou_threshold=nms_threshold)

			# Filter indices based on NMS.
			indices = tf.keras.backend.gather(indices, nms_indices)

		# Add indices to list of all indices.
		labels = tf.gather_nd(labels, indices)
		indices = tf.keras.backend.stack([indices[:, 0], labels], axis=1)

		return indices

	if class_specific_filter:
		all_indices = []
		# Perform per class filtering.
		for c in range(int(classification.shape[1])):
			scores = classification[:, c]
			labels = c * tf.ones((tf.keras.backend.shape(scores)[0],), dtype='int64')
			all_indices.append(_filter_detections(scores, labels))

		# Concatenate indices to single tensor.
		indices = tf.keras.backend.concatenate(all_indices, axis=0)
	else:
		scores  = tf.keras.backend.max(classification, axis = 1)
		labels  = tf.keras.backend.argmax(classification, axis = 1)
		indices = _filter_detections(scores, labels)

	# Select top k.
	scores              = tf.gather_nd(classification, indices)
	labels              = indices[:, 1]
	scores, top_indices = tf.nn.top_k(scores, k=tf.keras.backend.minimum(max_detections, tf.keras.backend.shape(scores)[0]))

	# Filter input using the final set of indices.
	indices = tf.keras.backend.gather(indices[:, 0], top_indices)
	boxes   = tf.keras.backend.gather(boxes, indices)
	labels  = tf.keras.backend.gather(labels, top_indices)
	other_  = [tf.keras.backend.gather(o, indices) for o in other]

	# Zero pad the outputs.
	pad_size = tf.keras.backend.maximum(0, max_detections - tf.keras.backend.shape(scores)[0])
	boxes    = tf.pad(boxes, [[0, pad_size], [0, 0]], constant_values=-1)
	scores   = tf.pad(scores, [[0, pad_size]], constant_values=-1)
	labels   = tf.pad(labels, [[0, pad_size]], constant_values=-1)
	labels   = tf.keras.backend.cast(labels, 'int32')
	other_   = [tf.pad(o, [[0, pad_size]] + [[0, 0] for _ in range(1, len(o.shape))], constant_values=-1) for o in other_]

	# Set shapes, since we know what they are.
	boxes.set_shape([max_detections, 4])
	scores.set_shape([max_detections])
	labels.set_shape([max_detections])
	for o, s in zip(other_, [list(tf.keras.backend.int_shape(o)) for o in other]):
		o.set_shape([max_detections] + s[1:])

	return [boxes, scores, labels] + other_


class FilterDetections(tf.keras.layers.Layer):
	""" Keras layer for filtering detections using score threshold and NMS.
	"""

	def __init__(
		self,
		nms                   = True,
		class_specific_filter = True,
		nms_threshold         = 0.5,
		score_threshold       = 0.05,
		max_detections        = 300,
		parallel_iterations   = 32,
		**kwargs
	):
		""" Filters detections using score threshold, NMS and selecting the top-k detections.
		Args
			nms                   : Flag to enable/disable NMS.
			class_specific_filter : Whether to perform filtering per class, or take the best scoring class and filter those.
			nms_threshold         : Threshold for the IoU value to determine when a box should be suppressed.
			score_threshold       : Threshold used to prefilter the boxes with.
			max_detections        : Maximum number of detections to keep.
			parallel_iterations   : Number of batch items to process in parallel.
		"""
		self.nms                   = nms
		self.class_specific_filter = class_specific_filter
		self.nms_threshold         = nms_threshold
		self.score_threshold       = score_threshold
		self.max_detections        = max_detections
		self.parallel_iterations   = parallel_iterations
		super(FilterDetections, self).__init__(**kwargs)

	def call(self, inputs, **kwargs):
		""" Constructs the NMS graph.
		Args
			inputs : List of [boxes, classification, other[0], other[1], ...] tensors.
		"""
		boxes          = inputs[0]
		classification = inputs[1]
		other          = inputs[2:]

		# Wrap nms with our parameters.
		def _filter_detections(args):
			boxes          = args[0]
			classification = args[1]
			other          = args[2]

			return filter_detections(
				boxes,
				classification,
				other,
				nms                   = self.nms,
				class_specific_filter = self.class_specific_filter,
				score_threshold       = self.score_threshold,
				max_detections        = self.max_detections,
				nms_threshold         = self.nms_threshold,
			)

		# Call filter_detections on each batch.
		outputs = tf.map_fn(
			_filter_detections,
			elems=[boxes, classification, other],
			dtype=[tf.keras.backend.floatx(), tf.keras.backend.floatx(), 'int32'] + [o.dtype for o in other],
			parallel_iterations=self.parallel_iterations
		)

		return outputs

	def compute_output_shape(self, input_shape):
		""" Computes the output shapes given the input shapes.
		Args
			input_shape : List of input shapes [boxes, classification, other[0], other[1], ...].
		Returns
			List of tuples representing the output shapes:
			[filtered_boxes.shape, filtered_scores.shape, filtered_labels.shape, filtered_other[0].shape, filtered_other[1].shape, ...]
		"""
		return [
			(input_shape[0][0], self.max_detections, 4),
			(input_shape[1][0], self.max_detections),
			(input_shape[1][0], self.max_detections),
		] + [
			tuple([input_shape[i][0], self.max_detections] + list(input_shape[i][2:])) for i in range(2, len(input_shape))
		]

	def compute_mask(self, inputs, mask=None):
		""" This is required in Keras when there is more than 1 output.
		"""
		return (len(inputs) + 1) * [None]

	def get_config(self):
		""" Gets the configuration of this layer.
		Returns
			Dictionary containing the parameters of this layer.
		"""
		config = super(FilterDetections, self).get_config()
		config.update({
			'nms'                   : self.nms,
			'class_specific_filter' : self.class_specific_filter,
			'nms_threshold'         : self.nms_threshold,
			'score_threshold'       : self.score_threshold,
			'max_detections'        : self.max_detections,
			'parallel_iterations'   : self.parallel_iterations,
		})

		return config
