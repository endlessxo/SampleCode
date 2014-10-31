from django.forms.util import ValidationError
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect, Http404, HttpResponse
from django.core.urlresolvers import reverse
from django.shortcuts import render_to_response, get_object_or_404
from django.template import loader
from django.template.context import RequestContext
from website.forms import Html5ModelForm, RichTextField, RichTextarea
from website._models.job import Job, Candidate
from website.helpers import get_profile, first, oembed, get_ats_integration
from django.contrib.auth.models import User, AnonymousUser
from website.views import paginate, generic_step1, branding
from django.contrib import messages
from website.helpers.exceptions import NotEntitledError, SocialAPIError,\
    PendingAsyncOperationError
from website._models import OAuthAccount, Attachment, spam_blacklist,\
    referral_source
from website.helpers.decorators.entitlement import entitlement
from website.helpers.decorators.usererror import usererror
from website._models.placement import VerificationResponse
from website.helpers.decorators.back_to_referer import back_to_referer
from website.views.social import hits_rollup, linkedin
from django.forms.widgets import Textarea, HiddenInput
from website.helpers.middleware.wizardmode import wizardmode
from django.forms.fields import BooleanField, CharField
from website import forms, helpers
from django.conf import settings
from django.template import defaultfilters
from website._models.location import LocationChoiceField, LocationWidget
import json
import re
from recaptcha.client import captcha
from django.db.models.aggregates import Sum
from website._models.radar_cache import PerJobRadarCache
from website._models.company import ATSCompanySettings, Category
from django.views.decorators.csrf import csrf_exempt
from website.views.silent_follow import gen_silent_follow_query_string
from website.helpers.decorators.prompt_hire import process_prompt_hire_form
from website._models.publish_message import PublishMessage
from website.views.promote import generic_promote
from website.forms import InputWithToolpitWidget
from django.views.decorators.http import require_POST
from website.helpers.tags import label
from website.helpers import mail
from website._models import abtest
from website.views.search import LocationRadiusSearchForm
from website.helpers.exceptions import ThrottleException
from website.helpers.decorators.consumer import consumer
from website.helpers.exceptions import ThrottleException
import sys
import urllib
from livesettings import config_value
from database import get_read_replica
from mobi.decorators import detect_mobile
#from website.helpers.decorators.cache import cache_header
from random import randint

def _locals(request, job_id, check_edit=True, check_share=False):
    profile = get_profile(request.user)
    job = None
    if job_id:
        job = get_object_or_404(Job, pk=job_id)
        if check_edit and not job.can_edit(request.user):
            raise NotEntitledError()
        if check_share and not job.can_share(request.user):
            raise NotEntitledError()
    return profile, job

@login_required
def skip(request, job_id):
    request.session["wizard_job"] = None
    job = get_object_or_404(Job, pk=job_id)
    return HttpResponseRedirect(reverse("job", args=[job.id, job.slug]))

@login_required
@usererror
@back_to_referer
def toggle(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    if not job.can_edit(request.user):
        raise NotEntitledError()
    job.is_open = not job.is_open
    if request.REQUEST.get("show_message", "True") == "True":
        if job.is_open:
            messages.success(request, "%s was set to open and is publicly available." % job)
        else:
            messages.success(request, "%s was closed and is no longer available publicly." % job)
    job.save()

    if request.REQUEST.get("email", "False") == "True":
        return HttpResponseRedirect(reverse("job", args=[job.id, job.slug]))

class Step1Form(Html5ModelForm):
    class Meta:
        model = Job
        fields = ("title", "employment_type", "industry", "category", "location", "salary", "bonus", "description", "tags", "external_id", "application_url", )
    spam_prevention = CharField(widget=HiddenInput(), required=False)
    location = LocationChoiceField(widget=LocationWidget(), label="Location", required=True)
    salary = CharField(label="Salary/Pay Rate", required=False, widget=InputWithToolpitWidget(attrs={
        "placeholder": "$120,000 a year or competitive",
        #"tooltip": {
        #    "class_name": "tooltip tooltip-job-salary",
        #    "message": defaultfilters.safe("Jobs that include a specific pay range receive <strong>58%</strong> more <strong>qualified</strong> job applications."),
        #    },
        }))
    description = RichTextField(label="Description", required=True, widget=RichTextarea())
    tags = forms.TagChoiceField(required=False, widget=forms.TagWidget(), label="Tags", help_text="Enter terms job seekers would use to search for this job.", )
    external_id = CharField(widget=HiddenInput(), required=False)

    def __init__(self, data=None, instance=None, request=None, *args, **kwargs):
        super(Step1Form, self).__init__(data, instance=instance, *args, **kwargs)
        self.request = request
        self._add_select_with_html("bonus", "referral bonuses", request.company.bonuses, reverse("leaders_manage_bonuses", args=[self.request.company.id]))
        if not request.entitlements.is_enabled("Referral Network"):
            try:
                del self.fields["bonus"]
            except KeyError:
                pass
        if request.company.permissions.enable_ats_applications:
            self.fields["application_url"].required = True
        else:
            self.fields["application_url"].widget = HiddenInput()
            self.fields["application_url"].required = False

    def _add_select_with_html(self, field_name, verbose_name_plural, queryset, edit_url, required=False):

        self.fields[field_name].required = False
        self.fields[field_name].extra_html = "Help text"
        self.fields[field_name].widget = forms.SelectWithExtraHtml()

        self.fields[field_name].queryset = queryset
        self.fields[field_name].required = required

        edit_link_text = "edit %s" % verbose_name_plural
        if not queryset:
            edit_link_text = "add %s" % verbose_name_plural

        edit_html = "<a href='%s' class='admin-edit-categories'>%s</a>" % (edit_url, edit_link_text)
        if self.request.company.is_admin(self.request.user):
            self.fields[field_name].widget.extra_html = defaultfilters.safe(edit_html)
        else:
            if not queryset:
                del self.fields[field_name]

    def clean_description(self):
        description = self.cleaned_data["description"]
        if spam_blacklist.is_spam(description):
            email = settings.EMAIL_CONTACT_US
            raise forms.ValidationError(defaultfilters.safe("This job description has triggered our spam filter. " \
                "If this is a legitimate job, please contact us at <a href='mailto:%(email)s?subject=Job Marked as Spam'>%(email)s</a>." % locals()))
        if not config_value("website", "ALLOW_LINKS_IN_JOBS"):
            if re.search('http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', description):
                raise forms.ValidationError(defaultfilters.safe("Sorry, links are not allowed in job descriptions." % locals()))
        return description

class Step1IndustryForm(Step1Form):
    def __init__(self, data=None, instance=None, request=None, *args, **kwargs):
        super(Step1IndustryForm, self).__init__(data, instance=instance, request=request, *args, **kwargs)
        del self.fields["category"]
        self.fields["industry"].required = True

class Step1CategoryForm(Step1Form):
    def __init__(self, data=None, instance=None, request=None, *args, **kwargs):
        super(Step1CategoryForm, self).__init__(data, instance=instance, request=request, *args, **kwargs)
        categories = Category.objects.filter(company=request.company)
        del self.fields["industry"]
        self._add_select_with_html(
            "category",
            "categories",
            categories,
            reverse("company_admin_categories", args=[request.company.id]),
            required=True if categories else False
            )

LOCATION_MIGATION_MESSAGE = "The location you originally entered is not recognized by the new location scheme in Reach. Please enter a new location that includes both a city and a state or country."

@login_required
@entitlement("Job Postings", lambda kwargs: kwargs.get("job_id") != None,
    "You must upgrade your edition to add more jobs.")
@usererror
@wizardmode(setting="wizard_job")
def step1(request, job_id=None, defaults=None, onsuccess=None):
    profile, job = _locals(request, job_id)
    employment_type = first(profile.employment_type.all())
    employment_type_id = employment_type.id if employment_type else None

    industry_id = None
    industries = profile.industries.all()
    if len(industries) == 1:
        industry = first(industries)
        industry_id = industry.id

    if job and not job.location and job.legacy_location:
        messages.error(request, LOCATION_MIGATION_MESSAGE)

    if not defaults:
        description = request.REQUEST.get("description", "") # for easier automated testing
        defaults = {"location": profile.location or "", "employment_type": employment_type_id, "industry": industry_id, "description": description, "spam_prevention": request.session.session_key} if profile else {}

    categories = Category.objects.filter(company=request.company)
    last_job = helpers.first(Job.objects.filter(user=request.user, category__isnull=False).order_by("-date_added"))
    if last_job:
        defaults["category"] = last_job.category

    integration = get_ats_integration(request.user)

    if not integration:
        request.session["ats_auth_next"] = reverse("user_jobs_ats", args=[request.user.id])

    if categories or request.company.is_admin(request.user):
        response = generic_step1(request, "job", job, Step1CategoryForm, defaults, {"request": request}, onsuccess=onsuccess)
    else:
        response = generic_step1(request, "job", job, Step1IndustryForm, defaults, {"request": request}, onsuccess=onsuccess)

    # "first_time" is added by signup.onestep().
    if "first_time" in request.session and request.method == "POST":
        del request.session["first_time"]

    return response

@login_required
@usererror
def promote(request, job_id):
    if "abtest1_first_time_job_id" in request.REQUEST:
        del request.session["abtest1_first_time_job_id"]
    job = get_object_or_404(Job, id=job_id)
    if not job.can_share(request.user):
        raise NotEntitledError()
    return generic_promote(request, job, "jobs/steps/promote.html",
        reverse("job", args=[job.id, job.slug]))

def oembed_view(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    return oembed.view(request, job)

#@cache_header(max_age=3600)
@process_prompt_hire_form
def view(request, job_id, slug=None):
    try:
        job = Job.objects.using(get_read_replica()).get(id=job_id)
        if job.spam_count > 999:
            raise Http404
    except:
        raise Http404

    if request.user.is_authenticated() and job.can_share(request.user):
        return view_private(request, job)

    return view_public(request, job)

@branding.branded_shortlink
@detect_mobile
def view_public(request, job):
    # facebook shortcut
    if request.GET.get("apply") == "true":
        return HttpResponseRedirect(reverse("job_apply", args=[job.id]))

    base_template = "jobs/abtests/9.html"
    if hasattr(request, "BRANDING"):
        base_template = "jobs/view_public_branded.html"
    else:
        # consumer experience on other recruiter's jobs, too
        if not job.can_edit(request.user):
            request.BASE_TEMPLATE = "consumer"

    profile = get_profile(job.user)
    
    company = helpers.get_company(profile)
    form = get_apply_form(request, job)
    request.session["javascript_captcha"] = False

    if request.user.is_authenticated() and helpers.in_same_company(request.user, job.user):
        coworker = request.user
        try:
            linkedin_account = helpers.first(OAuthAccount.objects.filter(user=request.user, type="linkedin"))
            recommendations = job.get_recommendations(request.user)
        except PendingAsyncOperationError:
            pass
        return render_to_response("jobs/view_public_coworker.html", RequestContext(request, locals()))

    # Commented items don't appear in any HTML template.
    context = {
        'profile': profile,
        'form': form,
        'html_captcha': defaultfilters.safe(captcha.displayhtml(settings.RECAPTCHA_PUB_KEY)),
        'company': company,
        'request': request,
        'job': job,
        'abtest_variation': abtest.get_variation(request, "public_job", job.id),
        'base_template': base_template,
        'silent_follow_query_string': gen_silent_follow_query_string(job.category, job.location),
        'permissions': company.permissions,
    }
    print " enabled :", config_value("job_ab_test", "AB_TESTING_ENABLED_FOR_PRO")
    include_in_abtest = False
    # Check if we should include pro/pro+ users in A/B test
    if config_value("job_ab_test", "AB_TESTING_ENABLED_FOR_PRO") and company.edition:
        if company.edition.is_paying:
            include_in_abtest = True

    if not company.edition or company.edition.is_grandfathered or include_in_abtest:
        if not company.edition or company.edition.is_grandfathered:
            context['search_form'] = LocationRadiusSearchForm()
        if "view_public.html" in base_template and not request.mobile:
            if config_value("job_ab_test", "AB_TESTING_ENABLED"):
                base_template = get_random_job_page_template(request)

                print "variation"
                print base_template

    print "  include in abtest : ", include_in_abtest
    print "     company edition: ", company.edition
    print "  ab testing enabled: ", config_value("job_ab_test", "AB_TESTING_ENABLED")
    praises = company.get_praise()
    
    
    if praises:
        context['praises'] = praises
        return render_to_response("jobs/view_public_referral.html", RequestContext(request, locals()))
    
    return render_to_response(base_template, RequestContext(request, context))

def get_random_job_page_template(request, return_variation_num_only=False):
    """Used for A/B testing different variations of the job landing page"""
    # print request.session["job_ab_test_variation"]
    # print dir(request.session)
    # print request.GET.get("abtest")
    variations = {}
    for i in xrange(0, 9):
        variation = "AB_TEST_%d" % i
        if config_value("job_ab_test", variation):
            variations[variation] = "jobs/abtests/%d.html" % i
    print variations
    if not len(variations) > 0:
        return "jobs/view_public.html"
    if "abtest" in request.GET:
        i = request.GET.get("abtest")
        if return_variation_num_only:
            return i
        else:
            return "jobs/abtests/%s.html" % i
    if "job_ab_test_variation" in request.session:

        # try:
        session_variation = request.session["job_ab_test_variation"]
        if session_variation:
            if return_variation_num_only:
                return session_variation[-1:]
            else:
                return variations[session_variation]
        # except:
        else:
            del request.session["job_ab_test_variation"]
            #Variation may be disabled, fall back to normal page
            return "jobs/view_public.html"


    session_variation = random.choice(variations.keys())
    request.session["job_ab_test_variation"] = session_variation
    return variations[session_variation]


@login_required
def view_private(request, job):
    candidates = Candidate.objects.filter(job=job)
    external_updates = hits_rollup(job=job) # graph
    contacts = _get_potential_candidates(request, job, job.location)
    user = job.user
    profile = job.user.profile
    enable_matches = config_value("matches_codes", "ENABLE_MATCHES")

    if job.external_id:
        integration = get_ats_integration(job.user)

    publish_history = []
    
    networks = ["facebook", "linkedin", "twitter"]
    if job.user == request.user:
        networks += ["simplyhired", "trovit", "juju"]
    
    updates = job.external_updates.filter(
        user=user,
        external_system__in=networks,
    )
    network_updates = dict((n, []) for n in networks)
    
    for update in updates:
        network_updates[update.external_system].append(update)
    
    user_account_types = set(user.oauth_accounts.values_list('type', flat=True))
    
    for network, jobs in network_updates.items():
        if len(jobs) > 0:
            last = jobs[0]
        else:
            last = None
        num_posts = len(jobs)
        
        if network in ["simplyhired", "trovit", "juju"]:
            enabled = profile.show_to_job_board(network)
            network_type = 'job_board'
        else:
            enabled = num_posts != 0
            network_type = 'social'
        
        publish_history.append({
            "name": network,
            "type": network_type,
            "num_posts": num_posts,
            "last_post": last.get_link() if last else None,
            "enabled": enabled,
            "views": get_job_account_views(job, network, user),
            "inquiries": get_job_account_applications(job, network, user),
            "connected": network in user_account_types,
        })

    job.current_user = request.user # used for syndication messages
    
    context = {
        'profile': profile,
        'contacts': contacts,
        'publish_history': publish_history,
        'request': request,
        'job': job,
        'candidates': candidates,
        'external_updates': external_updates,
    }
    
    return render_to_response("jobs/view_private.html", RequestContext(request, locals()))

def get_job_account_views(job, network, user):
    from website._models.stats import StatJobReferrer
    referrer = network + ".com"
    value = StatJobReferrer.objects.external().filter(user=user, job=job, referrer=referrer).aggregate(Sum("count")).get("count__sum", 0)
    return 0 if not value else value
# alternate implementation; real-time, but doesn't match graph
#    links = ShortLink.objects.filter(view_name="job", user=user, referrer=network)
#    return links.aggregate(view_count=Sum('hits'))["view_count"]

def get_job_account_applications(job, network, user):
    from website._models.referral_source import get_referral_source_domain
    referral_source_domain = get_referral_source_domain(network + ".com")
    return Candidate.objects.filter(owner=user, job=job, referral_source_domain=referral_source_domain).count()

def _get_potential_candidates(request, job, location):
    try:
        linkedin_account = helpers.first(OAuthAccount.objects.filter(user=request.user, type="linkedin"))
        return linkedin.search_for_candidates(linkedin_account, job, location=location)
    except (SocialAPIError, ThrottleException) as e:
        messages.error(request, e)

class MatchesForm(Html5ModelForm):
    class Meta:
        model = Job
        fields = ("tags", "location")

    tags = forms.TagChoiceField(required=False, widget=forms.TagWidget(), label="Tags", help_text="Enter terms job seekers would use to search for this job.", )
    location = LocationChoiceField(widget=LocationWidget(), label="Location", required=False)

@login_required
@entitlement("Matches")
@usererror
def matches(request, job_id):
    profile, job = _locals(request, job_id, check_edit=False, check_share=True)
    record = job
    linkedin_keywords = linkedin.keywords(job.title, job.tags.all())

    if profile.company.under_administration:
        has_referral_network = True

    location = request.session.get("job%s-location" % job_id, job.location)

    if request.method == "POST":

        form = MatchesForm(request.POST)

        if form.is_valid():

            tags = form.cleaned_data.get("tags")

            if job.can_edit(request.user):
                if tags != [unicode(tag.id) for tag in job.tags.all()]:
                    job.tags = tags
                    job.save()
                    PerJobRadarCache.objects.filter(job=job).delete()

            location = form.cleaned_data.get("location")
            request.session["job%s-location" % job_id] = location

            return HttpResponseRedirect(reverse("job_matches", args=[job.id]))
    else:
        # temporarily store the session's location in the job
        # instance, so the form displays it.  We don't want to
        # save the job with this change, though.
        job.location = location

        form = MatchesForm(instance=job)

    contacts = _get_potential_candidates(request, job, location)

    return render_to_response("jobs/steps/matches.html", RequestContext(request, locals()))


def list_user_closed(request, user_id):
    return list_user(request, user_id, False)

@process_prompt_hire_form
@consumer()
def list_user(request, user_id, is_open=True, tag_slug=None):
    if request.user.is_authenticated():

        if request.permissions.is_company_admin:
            return HttpResponseRedirect(reverse("company_admin_jobs", args=[request.company.id]))

        if request.user in request.company.get_users():
            return HttpResponseRedirect(reverse("mycompany_jobs"))

    user = get_object_or_404(User, id=user_id)
    profile = helpers.get_profile(user)
    _jobs = Job.objects.filter(user__id=user_id, is_open=is_open)
    if tag_slug:
        _jobs = _jobs.filter(tags__slug=tag_slug)
    jobs = paginate(request, _jobs)

    return render_to_response("jobs/list_public.html", RequestContext(request, locals()))

class StartYourApplicationForm(forms.Html5Form):
    name = CharField(required=True, max_length=100)
    email = forms.EmailStripField(required=True, max_length=50)
    spam_prevention = CharField(widget=HiddenInput(), required=False)

    def __init__(self, *args, **kwargs):
        self.mobile = kwargs.pop('mobile', False)
        
        is_employee = None

        if "is_employee" in kwargs:
            is_employee = kwargs.pop("is_employee")

        super(StartYourApplicationForm, self).__init__(*args, **kwargs)

        if is_employee:
            self.fields["name"].widget.attrs['readonly'] = True
            self.fields["email"].widget.attrs['readonly'] = True

class CandidateApplyForm(StartYourApplicationForm):
    phone = CharField()
    message = CharField(widget=Textarea(), required=True)
    source = CharField(widget=HiddenInput(), required=False)
    member = BooleanField(required=False)
    resume = forms.WhitelistFileField(widget=forms.FilePreviewInput(
        attrs=dict(size=10)  # for Firefox sizing of the input control
        ), required=False)
    appli = CharField(widget=HiddenInput(), required=False)


    def __init__(self, *args, **kwargs):
        result = super(CandidateApplyForm, self).__init__(*args, **kwargs)

        self.fields['phone'].required = config_value("website", "APPLICATION_REQUIRE_PHONE")
        
        if self.mobile:
            self.fields['resume'].required = False

        return result


    def clean_resume(self):
        value = self.cleaned_data['resume']
        if not value and not self.mobile:
            if config_value("website", "APPLICATION_REQUIRE_RESUME") and (self.cleaned_data.get('source') != "linkedin") and (self.cleaned_data.get('mobile') != "true"):
                raise ValidationError("A resume is required.")

        return value

    def clean_message(self):
        message = self.cleaned_data["message"]
        if re.search("<a href=\"https?://|\[link=https?://|\[url=https?://", message):
            email = settings.EMAIL_CONTACT_US
            raise forms.ValidationError(defaultfilters.safe("This message has triggered our spam filter. " \
                "If this is a legitimate message, please contact us at <a href='mailto:%(email)s?subject=Candidate Message Marked as Spam'>%(email)s</a>." % locals()))
        return message

def get_apply_form(request, job, apply_form_class=CandidateApplyForm, is_employee=False):
    job_display = "Job #%s: %s" % (job.id, job.title)
    if job.external_id:
        job_display = "Bullhorn Job #%s: %s" % (job.external_id, job.title)

    initials = {"message": "I'm interested in your \"%s\" job in %s. Please contact me about the position." % (job_display, job.location),
                "member": True}

    if request.user.is_authenticated():
        initials["name"] = helpers.name(request.user)
        initials["email"] = request.user.email
    else:
        account_profile = request.session.get("account_profile")
        if account_profile:
            initials["name"] = "%s %s" % (account_profile.get("first_name"), account_profile.get("last_name"))
            initials["email"] = account_profile.get("email")

    form = apply_form_class(initials, is_employee=is_employee, mobile=request.mobile)
    form.is_employee = is_employee
    form._errors = {}
    return form

@require_POST
def apply_inline(request):
    job_id = request.POST.get("job_id")
    return apply(request, job_id, template="jobs/_apply_inline.html", ajax=True)

def render_lnkdn(profile):

    # instantiate pystache renderer
    renderer = pystache.Renderer()

    # apply profile json set to resume template.
    resume = renderer.render_path('header.mustache', profile)
    resume += renderer.render_path('objective.mustache', profile)
 
    resume += renderer.render_path('experience.mustache', profile)
    for connection in profile['profile']['positions']['values']:
        connid = connection['company']['id']
        resume += renderer.render_path('position.mustache', connection)

    resume += renderer.render_path('honorsawards.mustache', profile)
    resume += renderer.render_path('footer.mustache', profile)

    return resume


@csrf_exempt # must be top decorator
@consumer()
@detect_mobile
def apply(request, job_id, apply_form_class=CandidateApplyForm, template=None, on_success=None, ajax=False, notify=True):
    job = get_object_or_404(Job, pk=job_id) 
    is_employee = request.user.is_authenticated() and helpers.in_same_company(request.user, job.user)
    request.session["javascript_captcha"] = False
    if request.method == "POST":
        form = apply_form_class(request.POST, request.FILES, is_employee=is_employee, mobile=request.mobile)
        if form.is_valid():
            spam = False

            # Check that the initial spam_prevention value has been
            # reversed by javascript
            if request.session.session_key and form.cleaned_data.get("spam_prevention") != request.session.session_key[::-1]:
                if request.POST.get('recaptcha_challenge_field'):
                    # If the spam prevention check fails, check the captcha in case
                    # the user has javascript disabled.  The captcha will be hidden
                    # if the user has javascript enabled.
                    check_captcha = captcha.submit(request.POST.get('recaptcha_challenge_field'),
                                                   request.POST.get('recaptcha_response_field'),
                                                   settings.RECAPTCHA_PRIVATE_KEY,
                                                   request.META['REMOTE_ADDR'])
                    if not check_captcha.is_valid:
                        spam = True
                        captcha_errors = "Please try the captcha again."
                        print "jobs.apply: suspected BOT! job_id=%s email=%s request=%s" % (job_id, form.cleaned_data.get("email"), request.META)
                else:
                    print "jobs.apply: suspected BOT! job_id=%s email=%s request=%s" % (job_id, form.cleaned_data.get("email"), request.META)
                    spam = True

            if not spam:
                name = form.cleaned_data.get("name")
                email = form.cleaned_data.get("email")
                phone = form.cleaned_data.get("phone")
                # Store the name and email so we can prepopulate the account signup form.
                request.session["new_profile_name"] = name
                request.session["new_profile_email"] = email

                candidate = first(Candidate.objects.filter(email__iexact=email, job=job))
                if not candidate:

                    candidate = Candidate()
                    candidate.owner = job.user
                    candidate.name = name
                    candidate.email = email
                    candidate.phone = phone
                    candidate.job = job
                    candidate.message = form.cleaned_data.get("message")
                    candidate.apply_version = "2"

                    if is_employee:
                        candidate.employee_user = request.user
                    else:
                        referral_user = helpers.get_referral_user(request)
                        # referral user could be job owner if candidate backed out of a job and hit another one
                        # also want to make sure users are not getting credit cross-company
                        if referral_user != job.user and helpers.in_same_company(referral_user, job.user):
                            candidate.referral_user = referral_user

                    candidate.member = "member" in request.POST
                    candidate.referral_source_domain = referral_source.get_referral_source_domain(request.COOKIES.get("referral_source"))
                    candidate.save()

                    appli = request.POST.get("appli", None)
                    if appli:
                        candidate.appli = json.loads(appli)

                        if not candidate.resume:
                            attachment = Attachment()
                            content = loader.render_to_string("candidates/appli.txt", candidate.appli).encode('utf-8')
                            from django.core.files.uploadedfile import SimpleUploadedFile
                            attachment.file = SimpleUploadedFile("%d-appli.txt" % candidate.id, content)
                            attachment.save()
                            candidate.resume = attachment
                        candidate.save()

                request.session["candidate_applied"] = candidate

                resume = request.FILES.get("resume")
                if resume:
                    attachment = Attachment()
                    attachment.file = resume
                    attachment.save()
                    candidate.resume = attachment
                    candidate.save()

                if notify:
                    candidate.copy_to_ats_and_send_email_notification()
                    company = helpers.get_company(user=job.user)
                    job_ab_test = ""
                    if not company.edition or company.edition.is_grandfathered:
                        if not request.mobile:
                            if config_value("job_ab_test", "AB_TESTING_ENABLED"):
                                job_ab_test = get_random_job_page_template(request, return_variation_num_only=True)
                                
                    simplyhired_confirm_html = loader.render_to_string("analytics/_simplyhired_conversion.html", {"job_ab_test": job_ab_test})
                    messages.success(request, defaultfilters.safe("Thanks! Your message has been sent! %s" % simplyhired_confirm_html))

                next = reverse("job_apply_thanks", args=[job_id])

                company = helpers.get_company(user=job.user)
                if not company.edition or company.edition.name == "Professional - Grandfathered":
                    linkedin_keywords = linkedin.keywords(job.title, job.tags.all())
                    location = None
                    if job.location is not None:
                        if job.location.state.country.name != "United States":
                            location = "%s, %s" % (job.location.city, job.location.state.country.name)
                        else:
                            location = "%s, %s" % (job.location.city, job.location.state.name)
                    search_query = {'q': linkedin_keywords, 'location': location}
                    next = helpers.update_url(reverse("jobs_landing_and_search"), search_query)

                try:
                    del request.session["job_ab_test_variation"]
                except:
                    pass
                if on_success:
                    return on_success(request, candidate, job)
                if hasattr(request, "BRANDING"):
                    next = reverse("home")
                if settings.DO_COMPANY_SURVEY:
                    next = reverse("survey") + "?job_id=%s" % job_id

                if ajax:
                    return HttpResponse(json.dumps({"next": next}), content_type="application/json")
                return HttpResponseRedirect(next)
            else:
                request.session["javascript_captcha"] = True
                if ajax:
                    return HttpResponse(json.dumps({"next": reverse("job", args=[job.id, job.slug])}), content_type="application/json")
    
    elif "applying_with_linkedin" in request.session:
        request.session["applying_with_linkedin"]
        account = request.session['account']
        linkedin_profile = request.session['account_profile']

        candidate = Candidate()
        candidate.owner = job.user
        referral_user = helpers.get_referral_user(request)
        
        # referral user could be job owner if candidate backed out of a job and hit another one
        # also want to make sure users are not getting credit cross-company

        if referral_user != job.user and helpers.in_same_company(referral_user, job.user):
            candidate.referral_user = referral_user
        
        candidate.member = "member" in request.POST
        candidate.referral_source_domain = referral_source.get_referral_source_domain(request.COOKIES.get("referral_source"))
        attachment = Attachment()
        candidate.appli = {}
        candidate.appli['person'] = {
        'lastName' : linkedin_profile['last_name'], 
        'firstName' : linkedin_profile['first_name'],
        'headline' : linkedin_profile['title'],
        'phoneNumbers' : linkedin_profile['phone_number'] ,
        'linkedin_phone_number' : linkedin_profile['phone_number'],
        'emailAddress' : linkedin_profile['email'],
        'publicProfileUrl' : linkedin_profile['profile_url'],
        }
        if (linkedin_profile['email']):
            candidate.appli['email'] = linkedin_profile['email']
        if (linkedin_profile['picture_url']):
            candidate.appli['person']['publicProfilePicUrl'] = linkedin_profile['picture_url']          
        if (linkedin_profile['work_history']):    
            candidate.appli['jobs'] = linkedin_profile['work_history']
        if (linkedin_profile['educations']):
            candidate.appli['educations'] = linkedin_profile['educations']
        if (linkedin_profile['description']):
            candidate.appli['person']['summary'] = linkedin_profile['description'],

        content = loader.render_to_string("candidates/appli.txt", candidate.appli).encode('utf-8')
        from django.core.files.uploadedfile import SimpleUploadedFile

        randomFileName = ('{firstName}{lastName}{randomNumber}.txt').format(firstName=candidate.appli['person']['firstName'], lastName=candidate.appli['person']['lastName'], randomNumber=str(randint(0,1000000)))
        attachment.file = SimpleUploadedFile(randomFileName, content)   #Generates the file name for the resume with a random integer from 0 - 1000k and full name
        attachment.save()
        candidate.resume = attachment

        candidate.name =  candidate.appli['person']['firstName'] + ' ' + candidate.appli['person']['lastName']
        candidate.email = candidate.appli['person']['emailAddress']
        candidate.phone = candidate.appli['person']['phoneNumbers']
        candidate.job =   job
        candidate.message='Applied from Apply with Linkedin Button' #This is the message that the recruiter can see.
        candidate.apply_version = "2" 

        if is_employee:
            candidate.employee_user = request.user
        else:
            referral_user = helpers.get_referral_user(request)
            if referral_user != job.user and helpers.in_same_company(referral_user, job.user):
                candidate.referral_user = referral_user

        candidate.member = "member" in request.POST
        candidate.referral_source_domain = referral_source.get_referral_source_domain(request.COOKIES.get("referral_source"))
        candidate.save()
      
        appli = request.POST.get("appli", None)

        del request.session["applying_with_linkedin"]
        request.session["candidate_applied"] = candidate

        next = reverse("job_apply_thanks", args=[job_id])
        company = helpers.get_company(user=job.user)

        company = helpers.get_company(user=job.user)
        if not company.edition or company.edition.name == "Professional - Grandfathered":
            linkedin_keywords = linkedin.keywords(job.title, job.tags.all())
            location = None
            if job.location is not None:
                if job.location.state.country.name != "United States":
                    location = "%s, %s" % (job.location.city, job.location.state.country.name)
                else:
                    location = "%s, %s" % (job.location.city, job.location.state.name)
            search_query = {'q': linkedin_keywords, 'location': location}
            next = helpers.update_url(reverse("jobs_landing_and_search"), search_query)
        try:
            del request.session["job_ab_test_variation"]
        except:
            pass
        if on_success:
            return on_success(request, candidate, job)
        if hasattr(request, "BRANDING"):
            next = reverse("home")
        if settings.DO_COMPANY_SURVEY:
            next = reverse("survey") + "?job_id=%s" % job_id
        if ajax:
            return HttpResponse(json.dumps({"next": next}), content_type="application/json")
        return HttpResponseRedirect(next)

        form = get_apply_form(request, job, apply_form_class=apply_form_class, is_employee=is_employee)
        form._errors = {}

    #Takes care of ~/job/1234/apply
    else: 
        form = get_apply_form(request, job, apply_form_class=apply_form_class, is_employee=is_employee)
        form._errors = {}

    html_captcha = defaultfilters.safe(captcha.displayhtml(settings.RECAPTCHA_PUB_KEY))

    if not template:
        template = "jobs/apply2.html"
        if hasattr(request, "BRANDING"):
            template = "jobs/apply2_branded.html"
    job_ab_test = get_random_job_page_template(request, return_variation_num_only=True)
    require_resume = config_value("website", "APPLICATION_REQUIRE_RESUME") and not request.mobile


    return render_to_response(template, RequestContext(request, locals()))

@consumer()
def thanks(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    profile = get_profile(job.user)
    accounts = OAuthAccount.objects.filter(user=job.user)

    existing_applications = []
    if request.user.is_authenticated():
        existing_applications = Candidate.objects.filter(email=request.user.email)
    more_jobs = []
    already_seen = []
    for tag in job.tags.all()[:3]:
        other_jobs = Job.objects.filter(tags=tag).exclude(id=job.id).exclude(id__in=[a.job.id for a in existing_applications])
        # http://jira/browse/TAL-771 for now, only show other jobs from THIS recruiter
        other_jobs = other_jobs.filter(user=job.user)
        other_jobs = [o for o in other_jobs[:10] if o not in already_seen]
        already_seen += other_jobs
        more_jobs.append({"tag": tag, "jobs": other_jobs})

    # show all jobs not matching a tag last
    other_jobs = Job.objects.filter(user=job.user).exclude(id__in=[a.id for a in already_seen])[:10]

    owner_as_list = [profile]
    abtest_variation = abtest.get_variation(request, "job_post_apply", job.id)
    return render_to_response("jobs/thanks.html", RequestContext(request, locals()))

@consumer()
@login_required
@detect_mobile
def post_preview(request):
    if request.method == "POST":
        try:
            form = Step1Form(request.POST, request=request)
            job = form.save(commit=False)
            job.user = request.user
            request.session["preview_job"] = job
        except Exception, e:
            print helpers.get_stacktrace(e)
            return HttpResponse(e, status=500)
        return HttpResponse("job saved", status=200)

    if request.method == "GET":

        job = request.session.get("preview_job")
        if not job:
            raise Http404()

        # This is a nasty hack and should be fixed... it was previously using sys.maxint,
        # which overflows a normal int in MySQL (PG did not have this problem). Temporarily
        # using the max value for a MySQL int.
        job.id = 2147483647  # need some valid integer to show apply sidebar

        form = get_apply_form(request, job)  # need to do this before anon

        user = request.user
        profile = request.user.get_profile()
        company = profile.company

        request.user = AnonymousUser()
        preview = True
        if not request.company.edition or request.company.edition.is_grandfathered:
            search_form = LocationRadiusSearchForm()

        return render_to_response("jobs/view_public.html", RequestContext(request, locals()))

def mark_as_spam(request, job_id):
    pass

@login_required
@back_to_referer
def stop_promoting(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    publish_message = PublishMessage.objects.get(job=job, user=request.user)
    publish_message.is_enabled = not publish_message.is_enabled
    publish_message.save()
    messages.success(request, "You have %s publishing %s" % ("resumed" if publish_message.is_enabled else "stopped", job))

def start_your_application(request, job_id):
    job = get_object_or_404(Job, pk=job_id)

    company = helpers.get_company(user=job.user)
    ats_company_settings = first(ATSCompanySettings.objects.filter(company=company))

    notify = False
    if ats_company_settings:
        notify = ats_company_settings.enable_new_candidate_notifications

    return apply(request, job_id, StartYourApplicationForm, "jobs/start_your_application.html",
                 on_success=close_fancybox_and_redirect, notify=notify)

def close_fancybox_and_redirect(request, candidate, job):
    permissions = helpers.get_company(user=candidate.owner).permissions
    mail.send_template(
        User(is_active=True, email=candidate.email),
        "Your application to: %s" % job.title,
        "email/framed_job_apply_email",
        locals(),
        )

    next = request.REQUEST.get("next")
    return helpers.close_lightbox(next_js="parent.window.location = '%(next)s';" % locals())

def apply_framed(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    company = helpers.get_company(user=job.user)

    if not branding.is_branded(request, company):
        return branding.redirect_branded(request, company)

    if not job.application_url:
        messages.error(request, "This job does not have an ATS application url.")
        return HttpResponseRedirect(reverse("job_apply", args=[job_id]))

    company = helpers.get_company(user=job.user)

    # allow admins to add dynamic variables to the framed page
    referral_source_domain = referral_source.get_referral_source_domain(request.COOKIES.get("referral_source"))
    context = {
        "name": defaultfilters.urlencode(label.name(job.user)),
        "email": defaultfilters.urlencode(job.user.email),
        "source": defaultfilters.urlencode(referral_source_domain.get_display_name() if referral_source_domain else ""),
        }
    final_apply_url = helpers.append_urls(job.application_url, getattr(company, "permissions.application_url_parameters", "") % context)

    return render_to_response("jobs/apply_framed.html", RequestContext(request, locals()))
