var SingleLinkModule = {};
var saveBufferSeconds = 0.5,
  timeouts = {};

$(document).ready(function(){
  SingleLinkModule.init();
  SingleLinkModule.setupEventHandlers();
});

SingleLinkModule.init = function () {
  SingleLinkModule.adjustTopMargin();
  if($('._isNewRecord')) { SingleLinkModule.handleNewRecord(); }
};

SingleLinkModule.handleNewRecord = function () {
  // On the new-record bar, update the width of the URL input to match its contents,
  // by copying the contents into a temporary span with the same class and measuring its width.

  var linkField = $('input.link-field');
  linkField.after("<span class='link-field'></span>");
  var linkSpan = $('span.link-field');
  linkSpan.text(linkField.val());
  linkField.width(linkSpan.width());
  linkSpan.remove();

}

SingleLinkModule.setupEventHandlers = function () {
  $("#details-button, .edit-link").click( function () {
    SingleLinkModule.handleShowDetails($(this));
    return false;
  });

  $("button.darchive").click( function(){
    SingleLinkModule.handleDarchiving($(this));
    return false;
  });

  $('#archive_upload_form')
    .submit(function(e) {
      e.preventDefault();
      var url = "/archives/"+archive.guid+"/";
      var data = {};
      data['file'] = $('#archive_upload_form').find('.file')[0].files[0];

      var requestArgs = {
        contentType: false,
        processData: false
      };
      if (window.FormData) {
        Helpers.sendFormData("PATCH", url, data, requestArgs)
        .done(function(data){
          location=location;
        });
      } else {
        $('#upload-error').text('Your browser version does not allow for this action. Please use a more modern browser.');
      }
    });

  $('#collapse-details')
    .find('input')
    .on('input propertychange change', function () {
      var inputarea = $(this);
      SingleLinkModule.saveInput(inputarea);
    });

  $('#collapse-details')
    .find('textarea')
    .on('textchange', function () {
      var textarea = $(this);
      SingleLinkModule.saveInput(textarea);
    });

  $(window).on('resize', function () { SingleLinkModule.adjustTopMargin(); });
}

SingleLinkModule.handleShowDetails = function () {
  $this = $("#details-button");
  var showingDetails = $this.text().match("Show");
  $this.text(showingDetails && showingDetails.length > 0 ? "Hide record details" : "Show record details");
  $('header').toggleClass('_activeDetails');
}

SingleLinkModule.adjustTopMargin = function () {
  var $wrapper = $('.capture-wrapper');
  var headerHeight = $('header').height();
  $wrapper.css('margin-top', headerHeight);
}

SingleLinkModule.handleDarchiving = function (context) {
  var $this = context;
  if (!$this.hasClass('disabled')){
    var prev_text = $this.text(),
      currently_private = prev_text.indexOf('Public') > -1,
      private_reason = currently_private ? null : $('select[name="private_reason"]').val() || 'user';

    $this.addClass('disabled');
    $this.text('Updating ...');

    Helpers.apiRequest('PATCH', '/archives/' + archive.guid + '/', {is_private: !currently_private, private_reason: private_reason}, {
      success: function(){
        window.location.reload(true);
      },
      error: function(jqXHR){
        $this.removeClass('disabled');
        $this.text(prev_text);
      }
    });
  }
}

SingleLinkModule.upload_form = function () {
  $('#linky-confirm').modal('hide');
  $('#upload-error').text('');
  $('#archive_upload_form').removeAttr('action').removeAttr('method');
  $('#archive-upload').modal('show');
  return false;
}

SingleLinkModule.saveInput = function (inputElement) {
  var statusElement = $('.save-status')[0];
  $(statusElement).html('Saving...');

  var data = {};
  data["title"] = $(inputElement[0]).val();
  Helpers.apiRequest("PATCH", '/archives/' + archive.guid + '/', data)
    .done(function(data){
      $(statusElement).html('Saved!');
    });
}
